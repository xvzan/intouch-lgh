from pathlib import Path
import struct
from intouch_lgh.index import inspect_idx_topology
from intouch_lgh.layout import inspect_archive_layout

FIXTURES = Path(__file__).parent / 'fixtures'


def test_two_public_idx_topologies():
    for stem, expected in [('24072200', 2), ('24072300', 17)]:
        report = inspect_idx_topology(FIXTURES / f'{stem}.idx')
        assert report['matches_observed_layout'], report['issues']
        assert report['leaf_count'] == expected
        assert report['root_physical_page'] == 6
        assert len(report['leaf_logical_ids_in_key_order']) == expected
        pair = inspect_archive_layout(FIXTURES / f'{stem}.lgh', FIXTURES / f'{stem}.idx')
        assert pair['idx']['topology']['matches_observed_layout']


def test_diagnostic_catches_bad_root_pointer(tmp_path):
    raw = bytearray((FIXTURES / '24072300.idx').read_bytes())
    # Page 6 is the observed root; its first u32 child id is at page+12.
    struct.pack_into('<I', raw, 6 * 512 + 12, 999)
    path = tmp_path / 'bad_root.idx'
    path.write_bytes(raw)
    report = inspect_idx_topology(path)
    assert not report['matches_observed_layout']
    assert any('non-leaf' in issue for issue in report['issues'])


def test_diagnostic_catches_bad_leaf_next(tmp_path):
    raw = bytearray((FIXTURES / '24072200.idx').read_bytes())
    # Page 2 leaf's offset +4 carries next logical leaf id (observed).
    struct.pack_into('<I', raw, 2 * 512 + 4, 999)
    path = tmp_path / 'bad_next.idx'
    path.write_bytes(raw)
    report = inspect_idx_topology(path)
    assert not report['matches_observed_layout']
    assert any('next=' in issue for issue in report['issues'])
