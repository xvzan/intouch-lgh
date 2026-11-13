from intouch_lgh.reader import LGHReader
from intouch_lgh.index import IDXReader
from intouch_lgh.audit import audit_archive_pair
"""Regression tests for physical-block audit and exact timestamp presentation."""
from pathlib import Path
import struct
import pytest
from intouch_lgh import FormatError
from intouch_lgh.reader import FILETIME_UNIX_EPOCH
from test_reader import make_block

FIXTURES = Path(__file__).parent / "fixtures"


def test_exact_seventh_digit_roundtrips():
    from intouch_lgh.query import to_filetime
    from intouch_lgh.reader import Record
    for fraction in (0, 1, 9, 10, 1234567, 9999999):
        ticks = FILETIME_UNIX_EPOCH + fraction
        record = Record("T", "REAL", 1.0, ticks, "fake", 0, 56, 0)
        assert to_filetime(record.timestamp_iso) == ticks
    assert Record("T", "REAL", 0., FILETIME_UNIX_EPOCH + 1234567, "fake", 0, 56, 0).timestamp_iso == "1970-01-01T00:00:00.1234567+00:00"


def test_indexed_records_strict_type_semantics(tmp_path):
    # Prepare an IDX copy from the genuine fixture, and mutate only a single
    # data value. The IDX name/block references remain valid.
    lgh = tmp_path / "modified.lgh"
    b = bytearray((FIXTURES / "24072300.lgh").read_bytes())
    idx = IDXReader(FIXTURES / "24072300.idx")
    entries = list(idx.entries())
    target = next(e for e in entries if e.tag == "MY_TAG3")
    block_start = target.lgh_offset
    assert b[block_start:block_start+4] == bytes.fromhex("ec130200")
    struct.pack_into("<d", b, block_start + 64, 2.5)  # invalid integer
    lgh.write_bytes(b)
    assert any(r.value == 2.5 for r in idx.records_for_tag(lgh, "MY_TAG3"))
    with pytest.raises(FormatError, match="Invalid INT"):
        list(idx.records_for_tag(lgh, "MY_TAG3", validate_types=True))


@pytest.mark.parametrize("stem", ["24072200", "24072300"])
def test_numeric_block_audit_counts_match_reader(stem):
    p = FIXTURES / f"{stem}.lgh"
    report = audit_archive_pair(p, FIXTURES / f"{stem}.idx")
    reader = LGHReader(p)
    numeric_blocks = list(reader.blocks())
    assert len(numeric_blocks) + report["metadata_blocks"] - 1 == report["indexed_blocks"]
    assert len(numeric_blocks) == len([b for b in numeric_blocks if b.value_type in ("BOOL", "INT", "REAL")])


def test_invalid_type_detected_by_reader_and_index_in_same_way(tmp_path):
    # Known on-disk f64 is retained but semantic coercion is opt-in.
    file = tmp_path / "example.lgh"
    file.write_bytes(make_block(opcode=bytes.fromhex("ec130100"), observations=[(FILETIME_UNIX_EPOCH, 3.0)]))
    assert list(LGHReader(file).records())[0].value == 3.0
    with pytest.raises(FormatError, match="Invalid BOOL"):
        list(LGHReader(file, validate_types=True).records())
