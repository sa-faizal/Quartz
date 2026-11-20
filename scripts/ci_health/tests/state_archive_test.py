# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

import gzip
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ci_health.state_archive import archive_state, main, restore_state


def test_roundtrip_is_deterministic_and_preserves_original_database(tmp_path):
    database = tmp_path / "health.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence (message TEXT)")
        connection.execute("INSERT INTO evidence VALUES (?)", ("retained history",))
    connection.close()
    original = database.read_bytes()
    archive = archive_state(tmp_path)
    first = archive.read_bytes()
    assert database.read_bytes() == original
    assert gzip.decompress(first) == original
    assert first[4:8] == b"\x00\x00\x00\x00"  # deterministic mtime
    assert first[3] & 8 == 0  # no source filename in gzip header
    assert archive_state(tmp_path).read_bytes() == first
    database.write_bytes(b"replace me")
    assert restore_state(tmp_path)
    assert database.read_bytes() == original
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT message FROM evidence").fetchone()[0]
            == "retained history"
        )
    connection.close()


@pytest.mark.parametrize("archive", [b"not gzip", gzip.compress(b"truncated")[:-3]])
def test_bad_archive_cannot_overwrite_existing_database(tmp_path, archive):
    database = tmp_path / "health.sqlite"
    database.write_bytes(b"previous good database")
    (tmp_path / "health.sqlite.gz").write_bytes(archive)
    with pytest.raises((OSError, EOFError)):
        restore_state(tmp_path)
    assert database.read_bytes() == b"previous good database"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "health.sqlite",
        "health.sqlite.gz",
    ]


def test_decompression_bound_is_enforced_atomically(tmp_path):
    database = tmp_path / "health.sqlite"
    database.write_bytes(b"previous")
    (tmp_path / "health.sqlite.gz").write_bytes(gzip.compress(b"a" * 101))
    with pytest.raises(ValueError, match="decompression size limit"):
        restore_state(tmp_path, max_bytes=100)
    assert database.read_bytes() == b"previous"
    assert restore_state(tmp_path, max_bytes=101)
    assert database.read_bytes() == b"a" * 101


def test_no_archive_preserves_migration_database_and_missing_directory(tmp_path):
    database = tmp_path / "health.sqlite"
    database.write_bytes(b"migration data")
    assert not restore_state(tmp_path)
    assert database.read_bytes() == b"migration data"
    assert not restore_state(tmp_path / "not-created")


def test_cli_bad_archive_returns_failure_and_keeps_database(tmp_path, capsys):
    (tmp_path / "health.sqlite").write_bytes(b"keep")
    (tmp_path / "health.sqlite.gz").write_bytes(b"broken")
    assert main(["restore", "--directory", str(tmp_path)]) == 1
    assert "CI health state:" in capsys.readouterr().err
    assert (tmp_path / "health.sqlite").read_bytes() == b"keep"
