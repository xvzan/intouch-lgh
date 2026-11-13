from pathlib import Path
import pytest
from intouch_lgh import FormatError
from intouch_lgh.audit import audit_archive_pair

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.mark.parametrize('stem,expected,blocks', [('24072200',412,34), ('24072300',4523,239)])
def test_real_pair_audit(stem, expected, blocks):
    result = audit_archive_pair(FIXTURES / f'{stem}.lgh', FIXTURES / f'{stem}.idx')
    assert result['numeric_records'] == expected
    assert result['indexed_blocks'] == blocks
    assert result['unknown_blocks'] == 0
    assert sum(result['tags'].values()) == expected


def test_corrupt_idx_detected(tmp_path):
    stem = '24072200'
    idx = tmp_path / 'broken.idx'
    contents = bytearray((FIXTURES / f'{stem}.idx').read_bytes())
    contents[1024:1026] = b'\xff\xff'
    idx.write_bytes(contents)
    with pytest.raises(FormatError):
        audit_archive_pair(FIXTURES / f'{stem}.lgh', idx)


def test_corrupt_lgh_detected(tmp_path):
    stem = '24072200'
    lgh = tmp_path / 'broken.lgh'
    contents = bytearray((FIXTURES / f'{stem}.lgh').read_bytes())
    contents[-512:-508] = b'BAD!'
    lgh.write_bytes(contents)
    with pytest.raises(FormatError):
        audit_archive_pair(lgh, FIXTURES / f'{stem}.idx')
