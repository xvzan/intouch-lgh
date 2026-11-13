from intouch_lgh.reader import LGHReader
"""Cross-sink fidelity: invalid float payloads never silently become NULL/NaN."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from examples.legacy_adapters import ExportValueError, write_jsonl, write_sqlite, write_dbapi, write_parquet, sync_sqlite_archive

FIXTURES = Path(__file__).parent / "fixtures"


def first_record():
    return next(LGHReader(FIXTURES / "24072200.lgh").records())


@pytest.mark.parametrize("bad", [float('nan'), float('inf'), -float('inf')])
def test_jsonl_default_rejects_nonfinite_atomically(tmp_path, bad):
    rec = first_record()
    changed = replace(rec, value=bad, raw_value_bits=None, record_offset=rec.record_offset + 16)
    target = tmp_path / 'keep.jsonl'
    target.write_text('ORIGINAL', encoding='utf-8')
    with pytest.raises(ExportValueError, match='NaN|Non-finite'):
        write_jsonl(iter([rec, changed]), target)
    assert target.read_text() == 'ORIGINAL'
    assert not list(tmp_path.glob('*.partial'))


def test_sqlite_default_rejects_nan_but_allows_infinities(tmp_path):
    rec = first_record()
    changed = replace(rec, value=float('nan'), raw_value_bits=None, record_offset=rec.record_offset + 16)
    db_path = tmp_path / 'test.db'
    with pytest.raises(ExportValueError, match='NaN'):
        write_sqlite([rec, changed], db_path, batch_size=1)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lgh_records').fetchone()[0] == 0
        with pytest.raises(ExportValueError, match='NaN'):
            write_dbapi([rec, changed], conn, batch_size=1)
    for bad in (float('inf'), -float('inf')):
        file = tmp_path / ('pos.db' if bad > 0 else 'neg.db')
        write_sqlite([replace(rec, value=bad, raw_value_bits=None)], file)
        with sqlite3.connect(file) as conn:
            assert conn.execute('SELECT value FROM lgh_records').fetchone()[0] == bad

def test_overflowing_filetime_refused_by_all_sinks(tmp_path):
    r = replace(first_record(), filetime_ticks=2**63)
    with pytest.raises(ExportValueError, match='signed int64'):
        write_jsonl([r], tmp_path / 'out.jsonl')
    with pytest.raises(ExportValueError, match='signed int64'):
        write_sqlite([r], tmp_path / 'db.sqlite')


def test_jsonl_values_and_time_match_sqlite_real_fixture(tmp_path):
    records = list(LGHReader(FIXTURES / '24072200.lgh').records())
    jl, db = tmp_path/'rows.jsonl', tmp_path/'rows.db'
    assert write_jsonl(records, jl) == write_sqlite(records, db) == len(records) == 412
    jrows = [json.loads(line) for line in jl.read_text(encoding='utf-8').splitlines()]
    with sqlite3.connect(db) as conn:
        rows = conn.execute('SELECT record_offset,value,filetime_ticks,timestamp_utc FROM lgh_records ORDER BY record_offset').fetchall()
    jrows.sort(key=lambda row: row['record_offset'])
    assert [(r['record_offset'], r['value'], r['filetime_ticks'], r['timestamp_utc']) for r in jrows] == rows


def test_parquet_accepts_nonfinite_and_preserves_previous_file_on_failure(tmp_path):
    pytest.importorskip('pyarrow')
    r = first_record()
    dest = tmp_path / 'out.parquet'
    assert write_parquet([r], dest) == 1
    saved = dest.read_bytes()
    write_parquet([r, replace(r, value=float('nan'), raw_value_bits=None)], dest, batch_size=1)
    import pyarrow.parquet as pq
    assert pq.read_table(dest)['value'].to_pylist()[0] == r.value
    assert len(pq.read_table(dest)['value']) == 2
    assert not list(tmp_path.glob('*.partial'))


def test_bad_value_in_real_file_rolls_back_archive_and_checkpoint(tmp_path):
    # Replace the first value with NaN using the observed <Qd> record shape.
    import struct
    raw = bytearray((FIXTURES / '24072200.lgh').read_bytes())
    rec = first_record()
    struct.pack_into('<d', raw, rec.record_offset + 8, float('nan'))
    modified = tmp_path / 'modified.lgh'
    modified.write_bytes(raw)
    db = tmp_path / 'archive.db'
    with pytest.raises(ExportValueError, match='NaN'):
        sync_sqlite_archive([modified], db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lgh_records').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM lgh_imports').fetchone()[0] == 0


def test_postgres_dbapi_mock_accepts_nonfinite():
    from examples.legacy_adapters import write_dbapi

    class Cursor:
        def __init__(self):
            self.written = []
            self.closed = False
        def execute(self, statement):
            assert 'CREATE TABLE' in statement or 'ALTER TABLE' in statement
        def executemany(self, statement, rows):
            self.written.extend(rows)
        def close(self):
            self.closed = True

    class Connection:
        def __init__(self):
            self.cur = Cursor()
            self.commits = 0
            self.rollbacks = 0
        def cursor(self):
            return self.cur
        def commit(self):
            self.commits += 1
        def rollback(self):
            self.rollbacks += 1

    conn = Connection()
    rec = first_record()
    assert write_dbapi([rec, replace(rec, value=float('inf'), raw_value_bits=None, record_offset=rec.record_offset + 16)], conn,
                       dialect='postgresql', batch_size=1) == 2
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn.cur.written[-1][4] == float('inf')
    assert conn.cur.closed
