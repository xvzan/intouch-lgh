from __future__ import annotations

from pathlib import Path
import shutil

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from intouch_lgh import Archive, FormatError
from intouch_lgh.parquet_adapter import export_parquet
from intouch_lgh.reader import LGHReader

FIXTURES = Path(__file__).parent / "fixtures"


def test_streaming_parquet_export_preserves_raw_data_and_physical_order(tmp_path):
    lgh = FIXTURES / "24072300.lgh"
    idx = lgh.with_suffix(".idx")
    destination = tmp_path / "archive.parquet"
    expected = list(LGHReader(lgh).records())

    with Archive.open(lgh, idx, validate="full") as archive:
        result = export_parquet(
            archive,
            destination,
            batch_size=333,
            row_group_size=500,
            compression="zstd",
        )

    table = pq.read_table(destination)
    assert result.path == destination.resolve()
    assert result.rows_written == len(expected) == table.num_rows
    assert result.batches_written > 1
    parquet_metadata = pq.ParquetFile(destination).metadata
    assert result.row_groups_written == parquet_metadata.num_row_groups
    row_group_rows = [
        parquet_metadata.row_group(i).num_rows
        for i in range(parquet_metadata.num_row_groups)
    ]
    assert sum(row_group_rows) == len(expected)
    assert all(size == 500 for size in row_group_rows[:-1])
    assert 0 < row_group_rows[-1] <= 500
    assert result.file_size_bytes == destination.stat().st_size

    assert table["tag"].to_pylist() == [r.tag for r in expected]
    assert table["value_type"].to_pylist() == [r.value_type for r in expected]
    assert table["filetime_ticks"].to_pylist() == [r.filetime_ticks for r in expected]
    assert table["value_bits"].to_pylist() == [r.value_bits for r in expected]
    assert table["record_index"].to_pylist() == [r.record_index for r in expected]
    assert table["block_offset"].to_pylist() == [r.block_offset for r in expected]
    assert table["record_offset"].to_pylist() == [r.record_offset for r in expected]

    metadata = table.schema.metadata or {}
    assert metadata[b"intouch_lgh.record_order"].startswith(b"LGH physical")
    assert metadata[b"intouch_lgh.value_bits"].startswith(b"authoritative raw")
    assert metadata[b"intouch_lgh.source_file"] == b"24072300.lgh"


def test_batch_larger_than_row_group_is_split_into_requested_groups(tmp_path):
    lgh = FIXTURES / "24072300.lgh"
    destination = tmp_path / "small-groups.parquet"

    with Archive.open(lgh, lgh.with_suffix(".idx"), validate="none") as archive:
        result = export_parquet(
            archive,
            destination,
            batch_size=1024,
            row_group_size=128,
            compression="zstd",
        )

    metadata = pq.ParquetFile(destination).metadata
    sizes = [metadata.row_group(i).num_rows for i in range(metadata.num_row_groups)]
    assert result.rows_written == metadata.num_rows
    assert sum(sizes) == metadata.num_rows
    assert all(size == 128 for size in sizes[:-1])
    assert 0 < sizes[-1] <= 128


def test_empty_selection_writes_valid_empty_parquet_with_schema(tmp_path):
    lgh = FIXTURES / "24072300.lgh"
    destination = tmp_path / "empty.parquet"
    with Archive.open(lgh, lgh.with_suffix(".idx"), validate="none") as archive:
        result = export_parquet(archive, destination, tags="__NO_SUCH_TAG__")

    table = pq.read_table(destination)
    assert result.rows_written == 0
    assert table.num_rows == 0
    assert table.column_names == [
        "tag", "value_type", "filetime_ticks", "value_bits", "value",
        "block_offset", "record_index", "record_offset",
    ]


def test_existing_destination_is_not_overwritten_by_default(tmp_path):
    lgh = FIXTURES / "24072300.lgh"
    destination = tmp_path / "existing.parquet"
    destination.write_bytes(b"KEEP-ME")

    with Archive.open(lgh, lgh.with_suffix(".idx"), validate="none") as archive:
        with pytest.raises(FileExistsError):
            export_parquet(archive, destination)

    assert destination.read_bytes() == b"KEEP-ME"
    assert not list(tmp_path.glob("*.partial"))


def test_late_parse_failure_does_not_replace_existing_destination(tmp_path):
    source_lgh = FIXTURES / "24072200.lgh"
    source_idx = FIXTURES / "24072200.idx"
    broken_lgh = tmp_path / "broken.lgh"
    broken_idx = tmp_path / "broken.idx"

    raw = bytearray(source_lgh.read_bytes())
    raw[-512:-508] = b"BAD!"
    broken_lgh.write_bytes(raw)
    shutil.copyfile(source_idx, broken_idx)

    destination = tmp_path / "preserve.parquet"
    destination.write_bytes(b"PREVIOUS-COMPLETE-EXPORT")

    with Archive.open(broken_lgh, broken_idx, validate="none") as archive:
        with pytest.raises(FormatError):
            export_parquet(
                archive,
                destination,
                batch_size=32,
                overwrite=True,
            )

    assert destination.read_bytes() == b"PREVIOUS-COMPLETE-EXPORT"
    assert not list(tmp_path.glob(".*.partial"))


def test_value_interpretation_requires_authoritative_bits(tmp_path):
    lgh = FIXTURES / "24072300.lgh"
    with Archive.open(lgh, lgh.with_suffix(".idx"), validate="none") as archive:
        with pytest.raises(ValueError, match="value_bits"):
            export_parquet(archive, tmp_path / "bad.parquet", columns=("value",))


def test_export_respects_tag_and_half_open_time_filters(tmp_path):
    lgh = FIXTURES / "24072300.lgh"
    idx = lgh.with_suffix(".idx")
    expected_all = list(LGHReader(lgh).records(tags={"MY_REAL"}))
    lo = expected_all[100].filetime_ticks
    hi = expected_all[300].filetime_ticks
    expected = [r for r in expected_all if lo <= r.filetime_ticks < hi]

    with Archive.open(lgh, idx, validate="none") as archive:
        result = export_parquet(
            archive,
            tmp_path / "filtered.parquet",
            tags="MY_REAL",
            start=lo,
            end=hi,
            batch_size=37,
        )

    table = pq.read_table(tmp_path / "filtered.parquet")
    assert result.rows_written == len(expected)
    assert table["record_offset"].to_pylist() == [r.record_offset for r in expected]
    assert table["value_bits"].to_pylist() == [r.value_bits for r in expected]


def test_parquet_source_metadata_can_be_omitted_or_preserve_full_path(tmp_path):
    lgh = FIXTURES / "24072300.lgh"
    with Archive.open(lgh, lgh.with_suffix(".idx"), validate="none") as archive:
        hidden_path = tmp_path / "hidden.parquet"
        export_parquet(archive, hidden_path, source_metadata="none")
        full_path = tmp_path / "full.parquet"
        export_parquet(archive, full_path, source_metadata="full")

    hidden_meta = pq.read_schema(hidden_path).metadata or {}
    full_meta = pq.read_schema(full_path).metadata or {}
    assert b"intouch_lgh.source_file" not in hidden_meta
    assert full_meta[b"intouch_lgh.source_file"] == str(lgh.resolve()).encode(
        "utf-8", errors="surrogateescape"
    )
