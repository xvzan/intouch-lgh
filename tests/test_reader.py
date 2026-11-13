import struct
from datetime import datetime, timezone
import sqlite3
import pytest
from intouch_lgh import FormatError
from examples.legacy_adapters import write_sqlite, write_jsonl
from intouch_lgh.reader import BLOCK_SIZE, FILETIME_UNIX_EPOCH, FILETIME_MAX_TICKS, Record
from intouch_lgh.reader import LGHReader
from intouch_lgh.query import to_filetime


def make_block(opcode=b'\xec\x13\x03\x00', tag='MY_TAG', observations=None):
    if observations is None:
        observations = [(FILETIME_UNIX_EPOCH, 1.25), (FILETIME_UNIX_EPOCH + 10, 2.5)]
    b = bytearray(BLOCK_SIZE)
    b[0:4] = opcode
    struct.pack_into('<I', b, 4, len(observations))
    struct.pack_into('<Q', b, 8, observations[0][0])
    b[16:56] = tag.encode().ljust(40, b'\0')
    for i, (ticks, value) in enumerate(observations):
        struct.pack_into('<Qd', b, 56+i*16, ticks, value)
    return bytes(b)


def test_records_precise_ticks(tmp_path):
    p = tmp_path/'sample.lgh'; p.write_bytes(make_block())
    rs = list(LGHReader(p).records())
    assert [r.value for r in rs] == [1.25, 2.5]
    assert [r.record_offset for r in rs] == [56, 72]
    assert rs[1].filetime_ticks - rs[0].filetime_ticks == 10
    assert rs[0].timestamp_utc == datetime(1970,1,1,tzinfo=timezone.utc)


def test_multi_block_and_zero_gap(tmp_path):
    p = tmp_path/'sample.lgh'; p.write_bytes(make_block() + bytes(512) + make_block(tag='NEXT'))
    assert [r.tag for r in LGHReader(p).records()] == ['MY_TAG','MY_TAG','NEXT','NEXT']
    assert [b.offset for b in LGHReader(p).blocks()] == [0, 1024]


def test_invalid_count(tmp_path):
    b = bytearray(make_block()); struct.pack_into('<I',b,4,29)
    p = tmp_path/'bad.lgh'; p.write_bytes(b)
    with pytest.raises(FormatError, match='count'):
        list(LGHReader(p).records())


def test_unknown_fails_closed(tmp_path):
    p = tmp_path/'bad.lgh'; p.write_bytes(b'ABCD' + bytes(508))
    with pytest.raises(FormatError, match='Unknown'):
        list(LGHReader(p).records())
    r = LGHReader(p,skip_unknown=True)
    assert list(r.records()) == [] and r.unknown_blocks == [0]


def test_truncation(tmp_path):
    p = tmp_path/'bad.lgh'; p.write_bytes(make_block()+b'x')
    with pytest.raises(FormatError, match='Truncated'):
        list(LGHReader(p).records())


def test_sinks(tmp_path):
    p = tmp_path/'sample.lgh'; p.write_bytes(make_block())
    db = tmp_path/'d.sqlite'
    assert write_sqlite(LGHReader(p).records(), db) == 2
    assert write_sqlite(LGHReader(p).records(), db, on_conflict="ignore") == 2
    with sqlite3.connect(db) as con:
        assert con.execute('SELECT count(*) FROM lgh_records').fetchone()[0] == 2
    jsonl = tmp_path/'out.jsonl'
    assert write_jsonl(LGHReader(p).records(), jsonl) == 2
    assert len(jsonl.read_text().splitlines()) == 2


def test_filetime_integer_validation_boundaries():
    zero = Record('T', 'REAL', 0.0, 0, 'fake', 0, 56, 0)
    maximum = Record('T', 'REAL', 0.0, FILETIME_MAX_TICKS, 'fake', 0, 56, 0)
    assert zero.timestamp_utc == datetime(1601, 1, 1, tzinfo=timezone.utc)
    assert maximum.timestamp_utc == datetime.max.replace(tzinfo=timezone.utc)
    invalid = Record('T', 'REAL', 0.0, FILETIME_MAX_TICKS + 1, 'fake', 0, 56, 0)
    with pytest.raises(FormatError, match='FILETIME'):
        _ = invalid.timestamp_utc


def test_reader_rejects_out_of_datetime_range_ticks(tmp_path):
    p = tmp_path / 'bad_time.lgh'
    p.write_bytes(make_block(observations=[(FILETIME_MAX_TICKS + 1, 1.0)]))
    with pytest.raises(FormatError, match='FILETIME'):
        list(LGHReader(p).records())


def test_iter_batches_preserves_order_and_exact_fields(tmp_path):
    observations = [(FILETIME_UNIX_EPOCH + i * 10, float(i) + 0.25) for i in range(5)]
    p = tmp_path / 'batch.lgh'
    p.write_bytes(make_block(observations=observations))
    reader = LGHReader(p)
    batches = list(reader.iter_batches(2))
    assert [len(batch) for batch in batches] == [2, 2, 1]
    flattened = [record for batch in batches for record in batch]
    direct = list(LGHReader(p).records())
    assert [
        (r.record_offset, r.filetime_ticks, r.value_bits)
        for r in flattened
    ] == [
        (r.record_offset, r.filetime_ticks, r.value_bits)
        for r in direct
    ]


def test_iter_batches_rejects_nonpositive_size(tmp_path):
    p = tmp_path / 'batch.lgh'
    p.write_bytes(make_block())
    with pytest.raises(ValueError, match='batch_size'):
        list(LGHReader(p).iter_batches(0))


def test_timestamp_iso_preserves_100ns_digit_and_round_trips():
    # One 100 ns tick after the Unix epoch cannot be represented by datetime,
    # but the exact ISO string and raw FILETIME value must remain recoverable.
    record = Record('T', 'REAL', 0.0, FILETIME_UNIX_EPOCH + 1, 'fake', 0, 56, 0)
    assert record.timestamp_utc == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert record.timestamp_iso == '1970-01-01T00:00:00.0000001+00:00'
    assert to_filetime(record.timestamp_iso) == record.filetime_ticks


def test_max_filetime_submicrosecond_ticks_are_accepted_and_preserved():
    # Python datetime truncates to microseconds, while timestamp_iso retains
    # the seventh 100 ns digit.
    maximum = FILETIME_MAX_TICKS
    record = Record('T', 'REAL', 0.0, maximum, 'fake', 0, 56, 0)
    assert record.timestamp_utc == datetime.max.replace(tzinfo=timezone.utc)
    assert record.timestamp_iso == '9999-12-31T23:59:59.9999999+00:00'
    assert to_filetime(record.timestamp_iso) == maximum


def test_pre_unix_epoch_100ns_timestamp_iso_round_trip():
    record = Record('T', 'REAL', 0.0, FILETIME_UNIX_EPOCH - 1, 'fake', 0, 56, 0)
    assert record.timestamp_iso == '1969-12-31T23:59:59.9999999+00:00'
    assert to_filetime(record.timestamp_iso) == record.filetime_ticks


def test_duplicate_timestamps_are_preserved_as_distinct_records(tmp_path):
    tick = FILETIME_UNIX_EPOCH + 10_000
    p = tmp_path / "same_timestamp.lgh"
    p.write_bytes(make_block(observations=[(tick, 1.25), (tick, 2.5)]))
    records = list(LGHReader(p).records())
    assert len(records) == 2
    assert [record.filetime_ticks for record in records] == [tick, tick]
    assert [record.value for record in records] == [1.25, 2.5]
