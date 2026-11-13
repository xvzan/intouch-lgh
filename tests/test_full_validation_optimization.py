from __future__ import annotations

from collections import Counter
from pathlib import Path
import struct

import pytest

from intouch_lgh import Archive, FormatError
from intouch_lgh.index import IDXReader

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("stem", ["24072200", "24072300"])
def test_one_pass_full_validation_matches_legacy_reference(stem):
    lgh = FIXTURES / f"{stem}.lgh"
    idx = lgh.with_suffix(".idx")

    # Reference the pre-optimization sequence: cross-check IDX entries with
    # random LGH header reads, then scan LGH blocks to build the Tag catalogue.
    legacy_index = IDXReader(idx)
    index_report = legacy_index.verify_lgh(lgh)
    legacy_counts = Counter()
    legacy_records = 0
    legacy_blocks = 0
    with Archive.open(lgh, idx, validate="none") as archive:
        for block in archive._scan_blocks():
            legacy_counts[block.tag] += block.count
            legacy_records += block.count
            legacy_blocks += 1

    with Archive.open(lgh, idx, validate="full") as archive:
        report = archive.validation_report
        assert dict(archive.tag_counts) == dict(legacy_counts)
        assert report.numeric_records == legacy_records
        assert report.numeric_blocks == legacy_blocks
        assert report.indexed_blocks == index_report["indexed_blocks"]
        assert report.index_pages == index_report["index_pages"]
        assert report.idx_entries_checked == report.indexed_blocks
        assert report.lgh_header_reads == report.indexed_blocks
        assert report.lgh_header_bytes_requested == report.indexed_blocks * 40
        assert report.lgh_blocks_scanned == report.lgh_size // 512


def test_one_pass_validation_detects_duplicate_idx_reference(tmp_path):
    source = FIXTURES / "24072200.idx"
    raw = bytearray(source.read_bytes())
    # Replace the second reference in the first leaf with the first one.
    first = struct.unpack_from("<I", raw, 2 * 512 + 12)[0]
    struct.pack_into("<I", raw, 2 * 512 + 16, first)
    bad_idx = tmp_path / "duplicate.idx"
    bad_idx.write_bytes(raw)

    with pytest.raises(FormatError, match="Duplicate IDX block references"):
        Archive.open(FIXTURES / "24072200.lgh", bad_idx, validate="full")


def test_one_pass_validation_preserves_coverage_error_precedence(tmp_path):
    source = FIXTURES / "24072200.idx"
    raw = bytearray(source.read_bytes())
    # Turn one valid leaf reference into the accepted zero/sentinel entry.
    # This leaves one LGH block uncovered without introducing an IDX error.
    struct.pack_into("<I", raw, 2 * 512 + 12, 0)
    entry_count = struct.unpack_from("<H", raw, 2 * 512 + 2)[0]
    first_name = 2 * 512 + 12 + 4 * entry_count
    old_names = []
    cursor = first_name
    for _ in range(entry_count):
        end = raw.index(0, cursor)
        old_names.append(bytes(raw[cursor:end]))
        cursor = end + 1
    replacement = b"\x7f\0" + b"".join(name + b"\0" for name in old_names[1:])
    page_end = 2 * 512 + 1024
    raw[first_name:page_end] = replacement + b"\0" * (page_end - first_name - len(replacement))
    bad_idx = tmp_path / "missing.idx"
    bad_idx.write_bytes(raw)

    # Also damage a different LGH block's opcode. The legacy validator reports
    # the coverage failure before scanning LGH block structure; the optimized
    # validator must preserve that public error precedence.
    lgh_raw = bytearray((FIXTURES / "24072200.lgh").read_bytes())
    lgh_raw[2 * 512:2 * 512 + 4] = b"BAD!"
    bad_lgh = tmp_path / "bad.lgh"
    bad_lgh.write_bytes(lgh_raw)

    with pytest.raises(FormatError, match="coverage mismatch"):
        Archive.open(bad_lgh, bad_idx, validate="full")


def test_one_pass_validation_keeps_idx_tag_mismatch_ahead_of_coverage(tmp_path):
    source = FIXTURES / "24072200.idx"
    raw = bytearray(source.read_bytes())
    page_offset = 2 * 512
    entry_count = struct.unpack_from("<H", raw, page_offset + 2)[0]

    # Replace the first leaf reference with the accepted zero/sentinel entry.
    struct.pack_into("<I", raw, page_offset + 12, 0)
    first_name = page_offset + 12 + 4 * entry_count
    names = []
    cursor = first_name
    for _ in range(entry_count):
        end = raw.index(0, cursor)
        names.append(bytes(raw[cursor:end]))
        cursor = end + 1
    replacement = b"\x7f\0" + b"".join(name + b"\0" for name in names[1:])
    page_end = page_offset + 1024
    raw[first_name:page_end] = replacement + b"\0" * (page_end - first_name - len(replacement))

    bad_idx = tmp_path / "missing-and-tag-mismatch.idx"
    bad_idx.write_bytes(raw)

    # Damage the Tag header of a different, still-indexed block. The old
    # verifier checked every IDX/LGH Tag relationship before reporting missing
    # coverage, and the optimized path must retain that precedence.
    second_number = struct.unpack_from("<I", source.read_bytes(), page_offset + 16)[0]
    lgh_raw = bytearray((FIXTURES / "24072200.lgh").read_bytes())
    tag_offset = (second_number - 1) * 512 + 16
    lgh_raw[tag_offset] ^= 0x01
    bad_lgh = tmp_path / "tag-mismatch.lgh"
    bad_lgh.write_bytes(lgh_raw)

    with pytest.raises(FormatError, match="IDX/LGH tag mismatch"):
        Archive.open(bad_lgh, bad_idx, validate="full")


def test_full_validation_preserves_non_utf8_codec_semantics():
    lgh = FIXTURES / "24072300.lgh"
    # utf-8-sig decodes the fixture's UTF-8 Tag strings identically but adds a
    # BOM when encoding; the optimized comparison must use decoded equality
    # for codecs where byte-level re-encoding is not canonical.
    with Archive.open(lgh, lgh.with_suffix(".idx"), validate="full", encoding="utf-8-sig") as archive:
        assert archive.validation_report.numeric_records == 4523
        assert sum(archive.tag_counts.values()) == 4523


def test_full_validation_does_not_treat_zero_opcode_nonzero_header_as_blank(tmp_path):
    source_idx = FIXTURES / "24072200.idx"
    raw_idx = bytearray(source_idx.read_bytes())
    page_offset = 2 * 512
    entry_count = struct.unpack_from("<H", raw_idx, page_offset + 2)[0]
    block_number = struct.unpack_from("<I", raw_idx, page_offset + 12)[0]
    names_start = page_offset + 12 + 4 * entry_count
    names = []
    cursor = names_start
    for _ in range(entry_count):
        end = raw_idx.index(0, cursor)
        names.append(bytes(raw_idx[cursor:end]))
        cursor = end + 1
    names[0] = b""
    encoded_names = b"".join(name + b"\0" for name in names)
    page_end = page_offset + 1024
    raw_idx[names_start:page_end] = encoded_names + b"\0" * (page_end - names_start - len(encoded_names))
    bad_idx = tmp_path / "empty-tag.idx"
    bad_idx.write_bytes(raw_idx)

    raw_lgh = bytearray((FIXTURES / "24072200.lgh").read_bytes())
    offset = (block_number - 1) * 512
    raw_lgh[offset:offset + 512] = b"\0" * 512
    struct.pack_into("<I", raw_lgh, offset + 4, 1)
    bad_lgh = tmp_path / "nonblank-zero-opcode.lgh"
    bad_lgh.write_bytes(raw_lgh)

    with pytest.raises(FormatError, match="Unknown LGH block type"):
        Archive.open(bad_lgh, bad_idx, validate="full")
