from intouch_lgh.reader import LGHReader
"""Conflicting record identity must never silently mask changed payloads."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import pytest
from examples.legacy_adapters import write_sqlite, write_dbapi

FIXTURES = Path(__file__).parent / "fixtures"


def sample():
    return next(LGHReader(FIXTURES / '24072200.lgh').records())


@pytest.mark.parametrize('writer', ['sqlite', 'dbapi'])
def test_changed_same_identity_fails_and_preserves_existing(tmp_path, writer):
    original = sample()
    changed = replace(original, value=original.value + 123.0)
    db = tmp_path / 'test.sqlite'
    if writer == 'sqlite':
        assert write_sqlite([original], db) == 1
        with pytest.raises(sqlite3.IntegrityError):
            write_sqlite([changed], db)
    else:
        with sqlite3.connect(db) as conn:
            assert write_dbapi([original], conn) == 1
            with pytest.raises(sqlite3.IntegrityError):
                write_dbapi([changed], conn)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT value FROM lgh_records').fetchone() == (original.value,)


def test_partial_insert_rolled_back_when_conflict_at_end(tmp_path):
    original = sample()
    fresh = replace(original, record_offset=original.record_offset + 16)
    changed = replace(original, value=42.0)
    db = tmp_path / 'test.sqlite'
    write_sqlite([original], db)
    with pytest.raises(sqlite3.IntegrityError):
        write_sqlite([fresh, changed], db, batch_size=1)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lgh_records').fetchone()[0] == 1


def test_legacy_ignore_requires_explicit_opt_in(tmp_path):
    original = sample()
    changed = replace(original, value=77.0)
    db = tmp_path / 'test.sqlite'
    write_sqlite([original], db)
    assert write_sqlite([changed], db, on_conflict='ignore') == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT value FROM lgh_records').fetchone() == (original.value,)
    with pytest.raises(ValueError, match='on_conflict'):
        write_sqlite([], db, on_conflict='overwrite')
    with sqlite3.connect(db) as conn:
        with pytest.raises(ValueError, match='on_conflict'):
            write_dbapi([], conn, on_conflict='overwrite')


def test_identical_replay_is_not_assumed_safe_by_basic_sink(tmp_path):
    record = sample()
    db = tmp_path / 'test.sqlite'
    write_sqlite([record], db)
    with pytest.raises(sqlite3.IntegrityError):
        write_sqlite([record], db)
