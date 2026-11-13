from pathlib import Path
from collections import Counter
import pytest
from intouch_lgh import FormatError
from intouch_lgh.index import IDXReader

FIXTURES = Path(__file__).parent / 'fixtures'

@pytest.mark.parametrize('stem,count,pages', [('24072200',34,3), ('24072300',239,18)])
def test_all_index_blocks_cross_reference(stem, count, pages):
    idx = IDXReader(FIXTURES / f'{stem}.idx')
    result = idx.verify_lgh(FIXTURES / f'{stem}.lgh')
    assert result == {'lgh_blocks':count+1, 'indexed_blocks':count, 'index_pages':pages}
    diagnostics = idx.last_verify_diagnostics
    assert diagnostics['idx_pages_parsed'] == pages
    assert diagnostics['idx_entries_checked'] == count
    assert diagnostics['lgh_header_reads'] == count
    assert diagnostics['lgh_header_bytes_requested'] == count * 40
    assert diagnostics['idx_parse_seconds'] >= 0
    assert diagnostics['idx_lgh_header_check_seconds'] >= 0
    assert diagnostics['idx_verify_seconds'] >= 0
    assert sum(Counter(e.tag for e in idx.entries()).values()) == count

@pytest.mark.parametrize('stem', ['24072200', '24072300'])
def test_tag_offsets_correspond_to_lgh(stem):
    idx = IDXReader(FIXTURES / f'{stem}.idx')
    path = FIXTURES / f'{stem}.lgh'
    with path.open('rb') as f:
        for tag in {e.tag for e in idx.entries()}:
            for number in idx.blocks_for_tag(tag):
                f.seek((number - 1)*512 + 16)
                assert f.read(40).split(b'\0',1)[0].decode() == tag

def test_detects_corrupted_index(tmp_path):
    raw = bytearray((FIXTURES / '24072200.idx').read_bytes())
    # Tamper with a leaf reference at page 2, preserving structural validity.
    raw[2*512+12:2*512+16] = (999).to_bytes(4,'little')
    p = tmp_path/'bad.idx'
    p.write_bytes(raw)
    with pytest.raises(FormatError, match='coverage mismatch'):
        IDXReader(p).verify_lgh(FIXTURES / '24072200.lgh')

@pytest.mark.parametrize('stem', ['24072200', '24072300'])
def test_indexed_records_equal_sequential(stem):
    from intouch_lgh.reader import LGHReader
    path = FIXTURES / f'{stem}.lgh'
    idx = IDXReader(FIXTURES / f'{stem}.idx')
    expected = list(LGHReader(path).records())
    for tag in {rec.tag for rec in expected}:
        actual = list(idx.records_for_tag(path, tag))
        want = [rec for rec in expected if rec.tag == tag]
        assert [(r.record_offset, r.filetime_ticks, r.value) for r in actual] == [
            (r.record_offset, r.filetime_ticks, r.value) for r in want]
