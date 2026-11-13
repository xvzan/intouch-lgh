from intouch_lgh.reader import LGHReader
from intouch_lgh.index import IDXReader
from intouch_lgh.audit import audit_archive_pair
"""Independent, stdlib-only byte-level checks of the two public fixtures.

These tests deliberately do not import the LGH parser to decode expected
records. They validate the *observed* layout, not a vendor specification.
"""
from collections import Counter
from pathlib import Path
import hashlib
import struct

import pytest

from intouch_lgh import FormatError

FIXTURES = Path(__file__).parent / 'fixtures'
NUMERIC = {b'\xec\x13\x01\x00': 'BOOL', b'\xec\x13\x02\x00': 'INT', b'\xec\x13\x03\x00': 'REAL'}


def scan_raw_lgh(path):
    """An intentionally separate offset-walker, not a call into LGHReader."""
    data = Path(path).read_bytes()
    assert len(data) % 512 == 0
    records = []
    op_counts = Counter()
    other = []
    for block_off in range(0, len(data), 512):
        block = memoryview(data)[block_off:block_off + 512]
        op = bytes(block[:4])
        if op not in NUMERIC:
            other.append((block_off, op))
            continue
        count = struct.unpack_from('<I', block, 4)[0]
        assert 1 <= count <= 28
        name = bytes(block[16:56]).split(b'\0', 1)[0].decode('utf-8')
        assert name
        op_counts[NUMERIC[op]] += 1
        for i in range(count):
            offset = block_off + 56 + 16*i
            ticks, bits = struct.unpack_from('<QQ', block, 56 + 16*i)
            records.append((offset, name, NUMERIC[op], ticks, bits))
    return records, op_counts, other


@pytest.mark.parametrize('stem,expected', [('24072200', 412), ('24072300', 4523)])
def test_independent_raw_walk_matches_reader_exact_bits(stem, expected):
    path = FIXTURES / (stem + '.lgh')
    raw, _, other = scan_raw_lgh(path)
    parsed = list(LGHReader(path).records())
    assert len(raw) == len(parsed) == expected
    for (offset, tag, kind, ticks, bits), rec in zip(raw, parsed, strict=True):
        assert (rec.record_offset, rec.tag, rec.value_type, rec.filetime_ticks, rec.value_bits) == (offset, tag, kind, ticks, bits)
    assert all(offset % 512 == 0 for offset, _ in other)


@pytest.mark.parametrize('stem', ['24072200', '24072300'])
def test_raw_byte_mutation_changes_exactly_one_observation(stem, tmp_path):
    original = FIXTURES / (stem + '.lgh')
    raw, _, _ = scan_raw_lgh(original)
    assert raw
    record_offset = raw[len(raw)//2][0]
    changed = bytearray(original.read_bytes())
    changed[record_offset+8] ^= 1  # change only one low bit of the encoded f64
    path = tmp_path / original.name
    path.write_bytes(changed)
    changed_raw, _, _ = scan_raw_lgh(path)
    old_records = list(LGHReader(original).records())
    new_records = list(LGHReader(path).records())
    differences = [i for i,(a,b) in enumerate(zip(old_records,new_records,strict=True)) if a.value_bits != b.value_bits]
    assert differences == [len(raw)//2]
    assert changed_raw[len(raw)//2][4] == (raw[len(raw)//2][4] ^ 1)


def test_unexpected_zero_block_is_not_a_hard_error_in_reader(tmp_path):
    """Document a potential silent omission; do not change format behavior yet."""
    original = FIXTURES / '24072200.lgh'
    data = bytearray(original.read_bytes())
    data[512:1024] = bytes(512)
    path = tmp_path / 'zero.lgh'
    path.write_bytes(data)
    # Current reader skips the block; paired IDX audit must detect the inconsistency.
    assert len(list(LGHReader(path).records())) <= 412
    assert 512 in [off for off in range(0,len(data),512) if data[off:off+512] == bytes(512)]
    with pytest.raises(FormatError):
        audit_archive_pair(path, FIXTURES / '24072200.idx')


def test_expected_files_have_no_all_zero_lgh_blocks():
    for stem in ('24072200', '24072300'):
        b = (FIXTURES / (stem + '.lgh')).read_bytes()
        assert all(any(b[i:i+512]) for i in range(0,len(b),512))
