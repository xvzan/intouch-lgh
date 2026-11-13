from pathlib import Path
import struct
from intouch_lgh.reader import OPCODES
from intouch_lgh.layout import inspect_archive_layout

FIXTURES = Path(__file__).parent / 'fixtures'


def test_real_samples_chronology_diagnostics():
    for name, expected in [('24072200', 412), ('24072300', 4523)]:
        report = inspect_archive_layout(FIXTURES / f'{name}.lgh')
        assert sum(v['records'] for v in report['tag_time_ranges'].values()) == expected
        assert report['chronology_inversions'] == []
        assert report['zero_block_offsets'] == []
        assert all(v['min_filetime_ticks'] <= v['max_filetime_ticks'] for v in report['tag_time_ranges'].values())


def test_detect_same_tag_timestamp_reversal_without_refusing_to_parse(tmp_path):
    raw = bytearray((FIXTURES / '24072200.lgh').read_bytes())
    offset = next(i for i in range(512, len(raw), 512)
                  if bytes(raw[i:i+4]) in OPCODES and int.from_bytes(raw[i+4:i+8], 'little') >= 2)
    first = struct.unpack_from('<Q', raw, offset + 56)[0]
    # Preserve the data-block header; introduce a reversal on its second record.
    struct.pack_into('<Q', raw, offset + 56 + 16, first - 1)
    path = tmp_path / 'reversed.lgh'
    path.write_bytes(raw)
    report = inspect_archive_layout(path)
    assert any(e['record_offset'] == offset + 72 and e['ticks'] == first - 1
               for e in report['chronology_inversions'])


def test_identify_adjacent_duplicate_timestamps(tmp_path):
    raw = bytearray((FIXTURES / '24072200.lgh').read_bytes())
    offset = next(i for i in range(512, len(raw), 512)
                  if bytes(raw[i:i+4]) in OPCODES and int.from_bytes(raw[i+4:i+8], 'little') >= 2)
    first = struct.unpack_from('<Q', raw, offset + 56)[0]
    struct.pack_into('<Q', raw, offset + 72, first)
    path = tmp_path / 'duplicate.lgh'
    path.write_bytes(raw)
    report = inspect_archive_layout(path)
    assert sum(report['adjacent_equal_timestamps_by_tag'].values()) >= 1


def test_zero_block_diagnostic_without_ascribing_semantics(tmp_path):
    raw = bytearray((FIXTURES / '24072200.lgh').read_bytes())
    # Do not interpret padding as records; report physical zero page if present.
    raw.extend(bytes(512))
    path = tmp_path / 'zero.lgh'
    path.write_bytes(raw)
    report = inspect_archive_layout(path)
    assert report['zero_block_offsets'] == [len(raw)-512]
