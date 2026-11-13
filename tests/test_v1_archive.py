from __future__ import annotations
from intouch_lgh.reader import LGHReader

from pathlib import Path
import shutil
import subprocess
import os
import sys
import pytest

from intouch_lgh import Archive, ArchiveChangedError, FormatError
from intouch_lgh.numpy_adapter import iter_numpy_batches

FIXTURES = Path(__file__).parent / "fixtures"


def _flatten(batches):
    rows = []
    for batch in batches:
        tags = batch.row_tags
        types = batch.row_value_types
        offsets = batch.record_offsets.tolist()
        for i in range(len(batch.filetime_ticks)):
            rows.append((
                tags[i], types[i], int(batch.filetime_ticks[i]),
                int(batch.value_bits[i]), int(batch.block_offsets[
                    # Recover the block row from block_starts.
                    next(j for j in range(len(batch.block_starts)-1)
                         if batch.block_starts[j] <= i < batch.block_starts[j+1])
                ]),
                offsets[i],
            ))
    return rows


def test_archive_full_validation_and_numpy_batch_equivalence():
    lgh = FIXTURES / "24072300.lgh"
    idx = lgh.with_suffix(".idx")
    expected = list(LGHReader(lgh).records())
    with Archive.open(lgh, idx, validate="full") as archive:
        assert archive.validation_report.level == "full"
        assert archive.validation_report.numeric_records == len(expected)
        batches = list(iter_numpy_batches(archive, batch_size=777))
        actual = _flatten(batches)
        want = [
            (r.tag, r.value_type, r.filetime_ticks, r.value_bits,
             r.block_offset, r.record_offset)
            for r in expected
        ]
        assert actual == want




def test_archive_tag_counts_are_collected_during_full_validation():
    lgh = FIXTURES / "24072300.lgh"
    idx = lgh.with_suffix(".idx")
    expected = {}
    for record in LGHReader(lgh).records():
        expected[record.tag] = expected.get(record.tag, 0) + 1

    with Archive.open(lgh, idx, validate="full") as archive:
        assert dict(archive.tag_counts) == expected
        assert sum(archive.tag_counts.values()) == archive.validation_report.numeric_records
        with pytest.raises(TypeError):
            archive.tag_counts["SHOULD_NOT_MUTATE"] = 1


def test_archive_tag_counts_reject_source_mutation_after_validation(tmp_path):
    source = FIXTURES / "24072300.lgh"
    lgh = tmp_path / source.name
    idx = tmp_path / source.with_suffix(".idx").name
    shutil.copyfile(source, lgh)
    shutil.copyfile(source.with_suffix(".idx"), idx)

    with Archive.open(lgh, idx, validate="full") as archive:
        assert archive.tag_counts
        with lgh.open("ab") as stream:
            stream.write(b"x")
        with pytest.raises(ArchiveChangedError):
            _ = archive.tag_counts


def test_archive_tag_counts_require_full_validation():
    lgh = FIXTURES / "24072300.lgh"
    with Archive.open(lgh, lgh.with_suffix(".idx"), validate="none") as archive:
        with pytest.raises(RuntimeError, match="full validation"):
            _ = archive.tag_counts
        report = archive.validate_full()
        assert report.level == "full"
        assert dict(archive.tag_counts)


def test_archive_multi_tag_tree_query_preserves_physical_order():
    lgh = FIXTURES / "24072300.lgh"
    idx = lgh.with_suffix(".idx")
    tags = {"MY_REAL", "MY_BOOL", "MY_INT"}
    expected = [r for r in LGHReader(lgh).records() if r.tag in tags]
    with Archive.open(lgh, idx, validate="none") as archive:
        actual = list(archive.iter_records(tags=tags))
        assert [r.record_offset for r in actual] == [r.record_offset for r in expected]
        batches = list(iter_numpy_batches(archive, tags=tags, batch_size=500))
        actual_offsets = [x for b in batches for x in b.record_offsets.tolist()]
        assert actual_offsets == [r.record_offset for r in expected]


def test_archive_time_filter_is_half_open_and_keeps_physical_order():
    lgh = FIXTURES / "24072300.lgh"
    idx = lgh.with_suffix(".idx")
    all_records = list(LGHReader(lgh).records(tags={"MY_REAL"}))
    lo = all_records[100].filetime_ticks
    hi = all_records[300].filetime_ticks
    expected = [r for r in all_records if lo <= r.filetime_ticks < hi]
    with Archive.open(lgh, idx, validate="none") as archive:
        actual = list(archive.iter_records(tags="MY_REAL", start=lo, end=hi))
        assert [r.record_offset for r in actual] == [r.record_offset for r in expected]
        batches = list(iter_numpy_batches(archive, 
            tags="MY_REAL", start=lo, end=hi, batch_size=64
        ))
        assert [x for b in batches for x in b.record_offsets.tolist()] == [
            r.record_offset for r in expected
        ]


def test_numpy_batch_uses_compact_block_metadata_and_readonly_arrays():
    lgh = FIXTURES / "24072300.lgh"
    with Archive.open(lgh, lgh.with_suffix(".idx"), validate="none") as archive:
        batch = next(iter_numpy_batches(archive, tags="MY_REAL", batch_size=4096))
        assert len(batch.block_offsets) <= len(batch.filetime_ticks)
        assert len(batch.block_starts) == len(batch.block_offsets) + 1
        assert len(batch.block_tag_ids) == len(batch.block_offsets)
        assert len(batch.type_codes) == len(batch.block_offsets)
        assert batch.values.dtype.name == "float64"
        assert batch.value_bits.dtype.name == "uint64"
        assert not batch.value_bits.flags.writeable
        assert not batch.values.flags.writeable
        assert len(batch.row_tags) == len(batch.filetime_ticks)
        assert len(batch.record_offsets) == len(batch.filetime_ticks)


def test_archive_empty_and_invalid_queries():
    lgh = FIXTURES / "24072300.lgh"
    with Archive.open(lgh, lgh.with_suffix(".idx"), validate="none") as archive:
        assert list(archive.iter_records(tags="NO_SUCH_TAG")) == []
        assert list(iter_numpy_batches(archive, tags=[])) == []
        with pytest.raises(ValueError, match="batch_size"):
            list(iter_numpy_batches(archive, batch_size=0))
        with pytest.raises(ValueError, match="end"):
            list(archive.iter_records(start=20, end=10))



def test_archive_identity_compares_path_and_fd_stat_baselines_separately(monkeypatch):
    # Simulate platforms where Path.stat() and os.fstat() expose different
    # timestamp metadata for the same file. The path's own metadata remains
    # stable between open and query, so the archive must still be readable.
    lgh = (FIXTURES / "24072300.lgh").resolve()
    idx = lgh.with_suffix(".idx")
    original_stat = Path.stat

    def shifted_path_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == lgh or path == idx:
            values = list(result)
            values[8] += 1.0  # mtime
            values[9] += 1.0  # ctime
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", shifted_path_stat)
    with Archive.open(lgh, idx, validate="none") as archive:
        first = next(archive.iter_records(tags="MY_REAL"))
        assert first.tag == "MY_REAL"


def test_archive_detects_file_change_during_lifetime(tmp_path):
    src = FIXTURES / "24072300.lgh"
    lgh = tmp_path / src.name
    idx = tmp_path / src.with_suffix(".idx").name
    shutil.copyfile(src, lgh)
    shutil.copyfile(src.with_suffix(".idx"), idx)
    with Archive.open(lgh, idx, validate="none") as archive:
        # The guard must accept the unchanged file before it detects a mutation.
        first = next(archive.iter_records(tags="MY_REAL"))
        assert first.tag == "MY_REAL"
        with lgh.open("ab") as f:
            f.write(b"x")
        with pytest.raises(ArchiveChangedError):
            list(archive.iter_records(tags="MY_REAL"))


def test_archive_raw_filetime_is_not_rejected_for_datetime_range():
    # The new raw API preserves any uint64 FILETIME. datetime conversion is a
    # separate convenience and may reject values outside datetime's range.
    lgh = FIXTURES / "24072300.lgh"
    idx = lgh.with_suffix(".idx")
    with Archive.open(lgh, idx, validate="none") as archive:
        batch = next(iter_numpy_batches(archive, tags="MY_REAL", batch_size=10))
        assert batch.filetime_ticks.dtype.name == "uint64"


def test_archive_closed_state():
    lgh = FIXTURES / "24072300.lgh"
    archive = Archive.open(lgh, lgh.with_suffix(".idx"), validate="none")
    archive.close()
    with pytest.raises(RuntimeError, match="closed"):
        list(archive.iter_records(tags="MY_REAL"))


def test_archive_can_close_after_early_batch_generator_stop():
    lgh = FIXTURES / "24072300.lgh"
    archive = Archive.open(lgh, lgh.with_suffix(".idx"), validate="none")
    batches = iter_numpy_batches(archive, batch_size=100)
    first = next(batches)
    assert len(first.filetime_ticks) > 0
    batches.close()
    # Returned arrays own their data; no live mmap export blocks close.
    archive.close()


def test_archive_metadata_tags_do_not_appear_as_numeric_records():
    lgh = Path(os.environ.get("INTOUCH_LGH_METADATA_TEST_LGH", "__optional_fixture_not_configured__.lgh"))
    idx = lgh.with_suffix(".idx")
    if not lgh.exists():
        pytest.skip("private large fixture not mounted")
    with Archive.open(lgh, idx, validate="none") as archive:
        assert list(archive.iter_records(tags="$HIST_CREATE")) == []
        assert list(iter_numpy_batches(archive, tags="$HIST_ROLLOVER")) == []


def test_raw_batches_preserve_exact_encoded_entry_bytes():
    lgh = FIXTURES / "24072300.lgh"
    idx = lgh.with_suffix(".idx")
    with Archive.open(lgh, idx, validate="none") as archive:
        batch = next(archive.iter_batches(tags="MY_REAL", batch_size=37))
        first_block = batch.blocks[0]
        with lgh.open("rb") as stream:
            stream.seek(first_block.block_offset + 56)
            expected = b"".join(
                stream.read(16) for _ in range(first_block.row_stop - first_block.row_start)
            )
        expected_ticks = b"".join(expected[i:i+8] for i in range(0, len(expected), 16))
        expected_bits = b"".join(expected[i+8:i+16] for i in range(0, len(expected), 16))
        start, stop = first_block.row_start, first_block.row_stop
        assert batch.filetime_ticks[start*8:stop*8] == expected_ticks
        assert batch.value_bits[start*8:stop*8] == expected_bits
        assert list(batch.iter_locations())[0][2] == (
            first_block.block_offset + 56 + batch.record_indices[first_block.row_start] * 16
        )


def test_core_raw_batch_api_does_not_import_numpy_or_pyarrow():
    code = f"""
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'numpy' or name.startswith('numpy.'):
        raise AssertionError('numpy imported by core')
    if name == 'pyarrow' or name.startswith('pyarrow.'):
        raise AssertionError('pyarrow imported by core')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from intouch_lgh import Archive, RawBatch
with Archive.open({str(FIXTURES / '24072300.lgh')!r},
                  {str(FIXTURES / '24072300.idx')!r},
                  validate='none') as archive:
    batch = next(archive.iter_batches(tags='MY_REAL', batch_size=20))
    assert isinstance(batch, RawBatch)
    assert len(batch.filetime_ticks) == len(batch) * 8
    assert len(batch.value_bits) == len(batch) * 8
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_core_raw_batch_api_works_without_numpy_import():
    code = f"""
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'numpy' or name.startswith('numpy.'):
        raise AssertionError('numpy imported by core')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from intouch_lgh import Archive, RawBatch
with Archive.open({str(FIXTURES / '24072300.lgh')!r},
                  {str(FIXTURES / '24072300.idx')!r},
                  validate='none') as archive:
    batch = next(archive.iter_batches(tags='MY_REAL', batch_size=20))
    assert isinstance(batch, RawBatch)
    assert len(batch.filetime_ticks) == len(batch) * 8
    assert len(batch.value_bits) == len(batch) * 8
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_iter_batches_releases_mmap_views_when_lgh_parse_fails(tmp_path):
    source_lgh = FIXTURES / "24072200.lgh"
    source_idx = FIXTURES / "24072200.idx"
    broken_lgh = tmp_path / "broken.lgh"
    broken_idx = tmp_path / "broken.idx"

    raw = bytearray(source_lgh.read_bytes())
    raw[-512:-508] = b"BAD!"
    broken_lgh.write_bytes(raw)
    shutil.copyfile(source_idx, broken_idx)

    archive = Archive.open(broken_lgh, broken_idx, validate="none")
    try:
        with pytest.raises(FormatError):
            list(archive.iter_batches(batch_size=32))
    finally:
        # This must not raise BufferError and mask the original FormatError.
        archive.close()


def test_iter_batches_can_be_closed_early_without_pinning_mmap():
    lgh = FIXTURES / "24072300.lgh"
    archive = Archive.open(lgh, lgh.with_suffix(".idx"), validate="none")
    batches = archive.iter_batches(tags="MY_REAL", batch_size=64)
    first = next(batches)
    assert len(first) == 64
    batches.close()
    archive.close()


def test_full_validation_exposes_phase_diagnostics():
    lgh = FIXTURES / "24072300.lgh"
    with Archive.open(lgh, lgh.with_suffix(".idx"), validate="full") as archive:
        report = archive.validation_report
        assert report.validation_wall_seconds is not None
        assert report.validation_wall_seconds >= 0
        assert report.validation_thread_cpu_seconds is not None
        assert report.validation_process_cpu_seconds is not None
        assert report.idx_parse_seconds is not None
        assert report.idx_lgh_header_check_seconds is not None
        assert report.idx_coverage_check_seconds is not None
        assert report.final_identity_check_seconds is not None
        # Full validation now checks only fixed-size block headers through
        # IDX references; the old second sequential 512-byte scan is gone.
        assert report.lgh_block_scan_seconds == 0.0
        assert report.idx_pages_parsed == report.index_pages
        assert report.idx_entries_checked == report.indexed_blocks
        assert report.lgh_header_reads == report.indexed_blocks
        assert report.lgh_header_bytes_requested == report.lgh_header_reads * 40
        assert report.lgh_block_header_bytes_read == report.indexed_blocks * 56 + 56
        # Logical coverage still spans the whole LGH, while only fixed-size
        # headers (plus any blank-block tails) need to be read for validation.
        assert report.lgh_logical_bytes_scanned == report.lgh_size
        assert report.lgh_block_header_bytes_read == report.indexed_blocks * 56 + 56
        assert report.lgh_blocks_scanned == report.lgh_size // 512
        assert report.numeric_records == sum(archive.tag_counts.values())
