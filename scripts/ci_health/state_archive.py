#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Atomically restore/archive the local CI health database for Git persistence."""

import argparse
import gzip
import os
import sys
import tempfile
from pathlib import Path

MAX_DATABASE_BYTES = 1_073_741_824
CHUNK_BYTES = 1_048_576


def restore_state(
    directory: str | Path, *, max_bytes: int = MAX_DATABASE_BYTES
) -> bool:
    """Restore a bounded archive; absence leaves an existing migration DB intact.

    A corrupt/truncated/oversized gzip never replaces the previous database.
    The archive remains on disk. Returns whether an archive was restored.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    directory = Path(directory)
    source, target = directory / "health.sqlite.gz", directory / "health.sqlite"
    if not source.exists():
        return False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
            temporary = Path(output.name)
            total = 0
            with gzip.open(source, "rb") as compressed:
                while True:
                    block = compressed.read(min(CHUNK_BYTES, max_bytes - total + 1))
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        raise ValueError(
                            "Restored database exceeds decompression size limit"
                        )
                    output.write(block)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        return True
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def archive_state(directory: str | Path) -> Path:
    """Write deterministic gzip atomically, preserving the original database."""
    directory = Path(directory)
    source, target = directory / "health.sqlite", directory / "health.sqlite.gz"
    temporary = None
    try:
        # Open the database first: a missing source must not disturb an archive.
        with source.open("rb") as database:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
                temporary = Path(output.name)
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=output, mtime=0
                ) as compressed:
                    while block := database.read(CHUNK_BYTES):
                        compressed.write(block)
                output.flush()
                os.fsync(output.fileno())
        os.replace(temporary, target)
        return target
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore", "archive"))
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "restore":
            restored = restore_state(args.directory)
            print(
                "Restored health.sqlite"
                if restored
                else "No archive; existing database preserved"
            )
        else:
            archive_state(args.directory)
            print("Archived health.sqlite.gz")
        return 0
    except (OSError, ValueError, EOFError) as exc:
        print(f"CI health state: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
