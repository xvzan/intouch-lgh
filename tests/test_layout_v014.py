from pathlib import Path
import struct
from intouch_lgh.layout import inspect_archive_layout

FIXTURES = Path(__file__).parent / 'fixtures'


def test_layout_public_fixtures():
    for day, blocks, records in [('24072200', 27, 412), ('24072300', 224, 4523)]:
        report = inspect_archive_layout(FIXTURES / f'{day}.lgh', FIXTURES / f'{day}.idx')
        assert report['numeric_blocks'] == blocks
        assert report['numeric_records'] == records
        assert report['header_first_timestamp_mismatch_offsets'] == []
        assert report['nonzero_unused_payload_offsets'] == []
        assert report['unknown_blocks'] == []
        assert report['idx']['page_kinds'][0] == 1
        assert len(report['idx']['internal_pages']) == 1
        assert report['idx']['coverage']['indexed_blocks'] == (34 if day == '24072200' else 239)


def test_detect_header_first_timestamp_mismatch(tmp_path):
    raw = bytearray((FIXTURES / '24072200.lgh').read_bytes())
    # First numeric block starts at offset 0x400 in the fixture.
    offset = next(i for i in range(512, len(raw), 512)
                  if raw[i:i+4] in [bytes.fromhex(s) for s in ('ec130100','ec130200','ec130300')])
    orig = struct.unpack_from('<Q', raw, offset + 8)[0]
    struct.pack_into('<Q', raw, offset + 8, orig + 1)
    path = tmp_path / 'mismatch.lgh'; path.write_bytes(raw)
    assert offset in inspect_archive_layout(path)['header_first_timestamp_mismatch_offsets']


def test_detect_nonzero_unused_tail(tmp_path):
    raw = bytearray((FIXTURES / '24072200.lgh').read_bytes())
    offset = next(i for i in range(512, len(raw), 512)
                  if raw[i:i+4] in [bytes.fromhex(s) for s in ('ec130100','ec130200','ec130300')]
                  and int.from_bytes(raw[i+4:i+8], 'little') < 28)
    count = int.from_bytes(raw[offset+4:offset+8], 'little')
    raw[offset+56+count*16] = 0x7a
    path = tmp_path / 'tail.lgh'; path.write_bytes(raw)
    assert offset in inspect_archive_layout(path)['nonzero_unused_payload_offsets']
