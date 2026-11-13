from intouch_lgh.index import IDXReader
"""Experimental IDX tree lookup: independent full-scan oracle, six real IDX files."""
from collections import defaultdict
from pathlib import Path
import os
import struct
import pytest

from intouch_lgh import FormatError

FIXTURES = Path(__file__).parent / 'fixtures'
OPTIONAL = Path(os.environ["INTOUCH_LGH_CORPUS_DIR"]) if os.environ.get("INTOUCH_LGH_CORPUS_DIR") else None
STEMS = tuple(sorted({"24072200", "24072300"} | (
    {p.stem for p in OPTIONAL.glob("*.idx")} if OPTIONAL is not None and OPTIONAL.is_dir() else set()
)))


def locate(stem):
    p = FIXTURES / f'{stem}.idx'
    if not p.exists():
        if OPTIONAL is not None:
            p = OPTIONAL / f'{stem}.idx'
    if not p.exists():
        pytest.skip(f'Optional local IDX fixture not available: {stem}')
    return p


@pytest.mark.parametrize('stem', STEMS)
def test_all_tag_tree_queries_match_full_scan(stem):
    reader = IDXReader(locate(stem))
    expected = defaultdict(list)
    for e in reader.entries():
        expected[e.tag].append(e.block_number)
    for tag, ids in expected.items():
        assert reader.blocks_for_tag(tag, method='tree') == sorted(ids), (stem, tag)
    for missing in ('', 'NON_EXISTENT_TAG_987654321', '~~~~~~'):
        assert reader.blocks_for_tag(missing, method='tree') == []


def test_default_scan_unchanged():
    reader = IDXReader(locate('24072300'))
    assert reader.blocks_for_tag('MY_REAL') == reader.blocks_for_tag('MY_REAL', method='scan')
    assert reader.blocks_for_tag('MY_REAL', method='tree') == reader.blocks_for_tag('MY_REAL')
    with pytest.raises(ValueError, match='method'):
        reader.blocks_for_tag('MY_REAL', method='bogus')


def test_tree_record_query_matches_scan():
    reader = IDXReader(locate('24072300'))
    lgh = FIXTURES / '24072300.lgh'
    scan = [(r.record_offset, r.filetime_ticks, r.value_bits) for r in reader.records_for_tag(lgh, 'MY_REAL', verify=False)]
    tree = [(r.record_offset, r.filetime_ticks, r.value_bits) for r in reader.records_for_tag(lgh, 'MY_REAL', verify=False, method='tree')]
    assert tree == scan


@pytest.mark.parametrize('offset,value', [(0x20, 0), (0x20, 0xffffffff), (0x20, 2), (0x04, 9)])
def test_invalid_tree_header_rejected(tmp_path, offset, value):
    raw = bytearray(locate('24072200').read_bytes())
    struct.pack_into('<I', raw, offset, value)
    bad = tmp_path / 'bad.idx'
    bad.write_bytes(raw)
    with pytest.raises(FormatError):
        IDXReader(bad).blocks_for_tag('MY_REAL', method='tree')


def test_tree_loop_rejected(tmp_path):
    raw = bytearray(locate('24072200').read_bytes())
    root_id = struct.unpack_from('<I', raw, 0x20)[0]
    root_offset = (root_id - 1) * 1024
    # Root first child now references itself.
    struct.pack_into('<I', raw, root_offset + 12, root_id)
    bad = tmp_path / 'loop.idx'
    bad.write_bytes(raw)
    with pytest.raises(FormatError, match='cycle'):
        IDXReader(bad).blocks_for_tag('MY_REAL', method='tree')
