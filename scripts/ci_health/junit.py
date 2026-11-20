# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Bounded JUnit ingestion. Reports are evidence, never executable content."""

import io
import math
import re
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree

MAX_XML_BYTES = 20_000_000


def _seconds(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def parse_junit(data: bytes, context: dict) -> list[dict]:
    """Parse testcase evidence, including nested suites and namespaced XML.

    Context supplies exact repo/run/attempt provenance. A failure message is
    bounded and contains only the diagnostic message attribute, never captured
    stdout/stderr or a full stack trace that might contain credentials.
    """
    if len(data) > MAX_XML_BYTES:
        raise ValueError("JUnit document exceeds size limit")
    # XML can be UTF-16/32. Strip NULs for this conservative declaration check;
    # ElementTree itself handles the encoding when parsing the original bytes.
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data.replace(b"\x00", b""), re.I):
        raise ValueError("JUnit DTD/entity declarations are forbidden")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ValueError("Invalid JUnit XML") from exc
    if _tag(root) not in {"testsuite", "testsuites"}:
        raise ValueError("XML is not a JUnit report")
    records = []

    def walk(element, suites, depth=0):
        if depth > 100:
            raise ValueError("JUnit nesting exceeds limit")
        tag = _tag(element)
        if tag == "testsuite":
            suites = suites + [element.get("name", "unnamed")]
        if tag == "testcase":
            if len(records) >= 100_000:
                raise ValueError("JUnit testcase count exceeds limit")
            children = {_tag(child): child for child in element}
            status = "pass"
            diagnostic = None
            for candidate, outcome in (
                ("error", "error"),
                ("failure", "fail"),
                ("skipped", "skipped"),
            ):
                if candidate in children:
                    status, diagnostic = outcome, children[candidate]
                    break
            records.append(
                {
                    **context,
                    "suite": " / ".join(suites) or "unnamed",
                    "name": element.get("name", "unnamed"),
                    "classname": element.get("classname", ""),
                    "status": status,
                    "duration_seconds": _seconds(element.get("time")),
                    "message": (
                        diagnostic.get("message", "")[:2000]
                        if diagnostic is not None
                        else ""
                    ),
                }
            )
            return
        for child in element:
            walk(child, suites, depth + 1)

    walk(root, [])
    return records


def parse_junit_archive(
    data: bytes,
    context: dict,
    *,
    max_bytes: int = MAX_XML_BYTES,
    max_files: int = 2000,
) -> list[dict]:
    """Read XML members in memory without extracting any archive path.

    Limits apply to the complete uncompressed archive, not merely XML files,
    and to bytes actually read. Unsafe paths and encrypted members are rejected.
    Non-JUnit XML is ignored; malformed JUnit is rejected rather than silently
    presented as a report containing zero failed tests.
    """
    if len(data) > max_bytes:
        raise ValueError("Artifact archive exceeds size limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid artifact ZIP") from exc
    with archive:
        members = archive.infolist()
        if len(members) > max_files:
            raise ValueError("Artifact archive has too many files")
        if sum(member.file_size for member in members) > max_bytes:
            raise ValueError("Uncompressed artifact exceeds size limit")
        records = []
        for member in members:
            path = PurePosixPath(member.filename.replace("\\", "/"))
            if (
                path.is_absolute()
                or ".." in path.parts
                or re.match(r"^[A-Za-z]:", member.filename)
                or member.flag_bits & 1
            ):
                raise ValueError("Unsafe artifact archive member")
            if member.is_dir() or path.suffix.lower() != ".xml":
                continue
            if member.file_size > MAX_XML_BYTES:
                raise ValueError("JUnit document exceeds size limit")
            with archive.open(member) as stream:
                xml = stream.read(min(max_bytes, MAX_XML_BYTES) + 1)
            if len(xml) > min(max_bytes, MAX_XML_BYTES):
                raise ValueError("JUnit document exceeds size limit")
            source = f'{context.get("source", "artifact")}#{member.filename}'
            try:
                records.extend(parse_junit(xml, {**context, "source": source}))
            except ValueError as exc:
                if str(exc) == "XML is not a JUnit report":
                    continue
                raise
        return records
