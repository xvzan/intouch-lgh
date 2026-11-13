from intouch_lgh.reader import LGHReader
from intouch_lgh.index import IDXReader
"""Raw IEEE-754 fidelity using actual public LGH sample with controlled edits."""
from dataclasses import replace
import json
import math
import sqlite3
import struct
from pathlib import Path

import pytest

from examples.legacy_adapters import ExportValueError, write_jsonl, write_sqlite

FIXTURES = Path(__file__).parent / 'fixtures'


def _patched(tmp_path, payload):
    original = FIXTURES / '24072300.lgh'
    first = next(LGHReader(original).records())
    data = bytearray(original.read_bytes())
    struct.pack_into('<Q', data, first.record_offset + 8, payload)
    target = tmp_path / '24072300.lgh'
    target.write_bytes(data)
    return target, first.record_offset


@pytest.mark.parametrize('bits', [
    0x7ff8000000000042,  # qNaN nonzero payload
    0xfff8000000000042,  # negative NaN payload
    0x7ff0000000000001,  # signaling NaN
    0x7ff0000000000000,  # positive infinity
    0xfff0000000000000,  # negative infinity
    0x8000000000000000,  # negative zero
])
def test_reader_preserves_raw_payload_bits(tmp_path, bits):
    path, off = _patched(tmp_path, bits)
    record = next(LGHReader(path).records())
    assert record.record_offset == off
    assert record.value_bits == bits
    if bits == 0x8000000000000000:
        assert record.value == 0.0 and math.copysign(1., record.value) < 0
    elif bits & 0x000fffffffffffff and (bits & 0x7ff0000000000000) == 0x7ff0000000000000:
        assert math.isnan(record.value)
    else:
        assert math.isinf(record.value)


def test_jsonl_bits_mode_preserves_nan_and_infinity(tmp_path):
    for bits in (0x7ff8000000000042, 0xfff0000000000000):
        path, _ = _patched(tmp_path, bits)
        rec = next(LGHReader(path).records())
        target = tmp_path / 'out.jsonl'
        assert write_jsonl([rec], target, nan_policy='bits') == 1
        row = json.loads(target.read_text().strip(), parse_constant=lambda x: pytest.fail(f'nonstandard JSON: {x}'))
        assert row['value'] is None
        assert int(row['value_bits_hex'], 16) == bits
        assert struct.unpack('<d', struct.pack('<Q', int(row['value_bits_hex'], 16)))[0] != 0


def test_sqlite_bits_mode_preserves_nan_without_silent_null(tmp_path):
    bits = 0x7ff8000000000042
    path, _ = _patched(tmp_path, bits)
    rec = next(LGHReader(path).records())
    db_path = tmp_path / 'out.sqlite'
    with pytest.raises(ExportValueError, match='NaN'):
        write_sqlite([rec], db_path)
    assert write_sqlite([rec], db_path, nan_policy='bits') == 1
    with sqlite3.connect(db_path) as con:
        value, raw = con.execute('SELECT value,value_bits FROM lgh_records').fetchone()
    assert value is None  # deliberate representation, not silent value substitution
    assert struct.unpack('<Q', raw)[0] == bits


def test_jsonl_ordinary_records_have_exact_raw_bits(tmp_path):
    rec = next(LGHReader(FIXTURES / '24072200.lgh').records())
    path = tmp_path / 'out.jsonl'
    write_jsonl([rec], path)
    row = json.loads(path.read_text())
    assert int(row['value_bits_hex'], 16) == rec.value_bits


def test_idx_reader_preserves_value_bits(tmp_path):
    bits = 0x7ff8000000000042
    lgh, offset = _patched(tmp_path, bits)
    rec = next(LGHReader(lgh).records())
    # Get the tag corresponding to the modified first observation.
    idx = IDXReader(FIXTURES / '24072300.idx')
    match = next(r for r in idx.records_for_tag(lgh, rec.tag, verify=True) if r.record_offset == offset)
    assert match.value_bits == bits


def test_reader_is_not_type_validator_for_unusual_bool_payload(tmp_path):
    src = FIXTURES / '24072300.lgh'
    bool_rec = next(r for r in LGHReader(src).records() if r.value_type == 'BOOL')
    data = bytearray(src.read_bytes())
    struct.pack_into('<Q', data, bool_rec.record_offset + 8, 0x7ff8000000000042)
    new = tmp_path / 'bool_nan.lgh'
    new.write_bytes(data)
    found = next(r for r in LGHReader(new).records() if r.record_offset == bool_rec.record_offset)
    assert math.isnan(found.value)
    assert found.value_bits == 0x7ff8000000000042
    with pytest.raises(Exception, match='Invalid BOOL'):
        _ = found.logical_value


def test_sqlite_migrates_v011_table_before_preserving_bits(tmp_path):
    db = tmp_path / 'old.sqlite'
    with sqlite3.connect(db) as con:
        con.execute('''CREATE TABLE lgh_records (
            source_file TEXT NOT NULL, record_offset INTEGER NOT NULL,
            tag TEXT NOT NULL, value_type TEXT NOT NULL, value REAL,
            filetime_ticks INTEGER NOT NULL, timestamp_utc TEXT NOT NULL,
            block_offset INTEGER NOT NULL, record_index INTEGER NOT NULL,
            PRIMARY KEY(source_file, record_offset))''')
    rec = next(LGHReader(FIXTURES / '24072200.lgh').records())
    assert write_sqlite([rec], db) == 1
    with sqlite3.connect(db) as con:
        assert 'value_bits' in {row[1] for row in con.execute('PRAGMA table_info(lgh_records)')}
        assert struct.unpack('<Q', con.execute('SELECT value_bits FROM lgh_records').fetchone()[0])[0] == rec.value_bits


def test_postgres_archive_keeps_bit_hex_in_dbapi_adapter(tmp_path):
    from test_postgres_archive import PgConnectionAdapter
    from examples.legacy_adapters import sync_postgres_archive_connection
    bits = 0x7ff8000000000042
    path, off = _patched(tmp_path, bits)
    conn = PgConnectionAdapter()
    try:
        assert list(sync_postgres_archive_connection([path], conn).values()) == [4523]
        row = conn.db.execute('SELECT value_bits_hex FROM lgh_records WHERE record_offset=?', (off,)).fetchone()
        assert row == (f'{bits:016x}',)
    finally:
        conn.close()


def test_postgres_dbapi_writes_nan_with_bits_sidecar():
    from examples.legacy_adapters import write_dbapi
    class Cur:
        def __init__(self):
            self.commands = []
            self.rows = []
        def execute(self, sql):
            self.commands.append(sql)
        def executemany(self, sql, rows):
            self.commands.append(sql)
            self.rows.extend(rows)
        def close(self):
            pass
    class Conn:
        def __init__(self):
            self.cur = Cur()
            self.commits = 0
            self.rollbacks = 0
        def cursor(self):
            return self.cur
        def commit(self):
            self.commits += 1
        def rollback(self):
            self.rollbacks += 1
    rec = next(LGHReader(FIXTURES / '24072200.lgh').records())
    bits = 0x7ff8000000000042
    rec = replace(rec, value=float('nan'), raw_value_bits=bits)
    con = Conn()
    assert write_dbapi([rec], con, dialect='postgresql') == 1
    assert con.commits == 1 and con.rollbacks == 0
    assert con.cur.rows[0][-1] == f'{bits:016x}'
    assert any('value_bits_hex' in sql for sql in con.cur.commands)
