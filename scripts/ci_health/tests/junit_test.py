# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ci_health.junit import parse_junit, parse_junit_archive


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return output.getvalue()


def test_nested_suite_evidence_and_no_stdout():
    records = parse_junit(
        b"""<testsuites><testsuite name="outer"><testsuite name="inner">
        <testcase name="a" classname="Case" time="1.25"/>
        <testcase name="b"><failure message="assertion">stack trace</failure>
        <system-out>secret output</system-out></testcase>
        <testcase name="c"><skipped/></testcase>
        <testcase name="d" time="NaN"><error message="crash"/></testcase>
        </testsuite></testsuite></testsuites>""",
        {"repo": "ROCm/TheRock", "run_id": 10, "run_attempt": 2},
    )
    assert [record["status"] for record in records] == [
        "pass",
        "fail",
        "skipped",
        "error",
    ]
    assert records[0]["suite"] == "outer / inner"
    assert records[0]["duration_seconds"] == 1.25
    assert records[3]["duration_seconds"] is None
    assert records[1]["message"] == "assertion"
    assert "secret" not in str(records)
    assert all(record["run_attempt"] == 2 for record in records)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_reject_entity_declarations(encoding):
    xml = (
        '<!DOCTYPE x [<!ENTITY e "bad">]><testsuite><testcase name="&e;"/></testsuite>'
    )
    with pytest.raises(ValueError, match="DTD/entity"):
        parse_junit(xml.encode(encoding), {})


def test_archive_in_memory_and_non_junit_xml():
    data = archive(
        {
            "results/test.xml": '<testsuite name="s"><testcase name="x"/></testsuite>',
            "config.xml": "<configuration/>",
        }
    )
    records = parse_junit_archive(data, {"source": "artifact:9"})
    assert len(records) == 1
    assert records[0]["source"] == "artifact:9#results/test.xml"


@pytest.mark.parametrize("path", ["../evil.xml", "/evil.xml", "C:\\evil.xml"])
def test_archive_rejects_unsafe_paths(path):
    with pytest.raises(ValueError, match="Unsafe"):
        parse_junit_archive(archive({path: "<testsuite/>"}), {})


def test_archive_uncompressed_limit_and_member_limit():
    data = archive({"large.txt": "x" * 10000})
    with pytest.raises(ValueError, match="Uncompressed"):
        parse_junit_archive(data, {}, max_bytes=1000)
    with pytest.raises(ValueError, match="too many"):
        parse_junit_archive(archive({"a": "x", "b": "y"}), {}, max_files=1)


def test_malformed_xml_not_silently_healthy():
    with pytest.raises(ValueError, match="Invalid JUnit"):
        parse_junit_archive(archive({"junit.xml": "<testsuite>"}), {})
