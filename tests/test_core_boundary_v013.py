"""Core surface and database integration stay separate."""
from pathlib import Path
import importlib
import sqlite3

import intouch_lgh


def test_core_does_not_publish_sink_apis():
    for name in ('write_sqlite', 'write_postgres', 'write_parquet',
                 'write_dbapi', 'sync_sqlite_archive', 'ExportValueError'):
        assert not hasattr(intouch_lgh, name)


def test_external_sqlite_example(tmp_path):
    from examples.sqlite3_insert import import_lgh
    src = Path(__file__).parent / 'fixtures' / '24072200.lgh'
    dest = tmp_path / 'history.sqlite'
    assert import_lgh(str(src), str(dest)) == 412
    with sqlite3.connect(dest) as db:
        assert db.execute('SELECT COUNT(*) FROM history').fetchone()[0] == 412
        assert db.execute('SELECT length(value_bits) FROM history LIMIT 1').fetchone()[0] == 8


def test_core_readers_keep_exact_record_bits():
    src = Path(__file__).parent / 'fixtures' / '24072300.lgh'
    rec = next(intouch_lgh.reader.LGHReader(src).records())
    assert isinstance(rec.filetime_ticks, int)
    assert isinstance(rec.value_bits, int)
