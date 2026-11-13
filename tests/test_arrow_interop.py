from __future__ import annotations

from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from intouch_lgh import Archive
from intouch_lgh.arrow_adapter import iter_arrow_batches

FIXTURES = Path(__file__).parent / "fixtures"
COLUMNS = (
    "tag",
    "value_type",
    "filetime_ticks",
    "value_bits",
    "value",
    "record_index",
    "block_offset",
    "record_offset",
)


def _fixture_table(batch_size: int = 257):
    lgh = FIXTURES / "24072300.lgh"
    idx = lgh.with_suffix(".idx")
    with Archive.open(lgh, idx, validate="full") as archive:
        batches = list(iter_arrow_batches(
            archive,
            batch_size=batch_size,
            columns=COLUMNS,
        ))
    assert batches
    return pa.Table.from_batches(batches)


def test_pyarrow_parquet_roundtrip_preserves_raw_columns_and_order(tmp_path):
    table = _fixture_table()
    path = tmp_path / "roundtrip.parquet"

    # Multiple batches deliberately exercise per-batch dictionary contents.
    assert table.num_rows > 0
    assert table.column("tag").num_chunks > 1

    pq.write_table(table, path, compression="zstd")
    restored = pq.read_table(path)

    assert restored.num_rows == table.num_rows
    assert restored.column_names == table.column_names

    # Exact raw integer columns are authoritative. Do not use the float column
    # to reconstruct IEEE-754 payload bits after crossing analytics libraries.
    for name in ("filetime_ticks", "value_bits", "record_index",
                 "block_offset", "record_offset"):
        assert restored[name].to_pylist() == table[name].to_pylist()

    assert restored["tag"].to_pylist() == table["tag"].to_pylist()
    assert restored["value_type"].to_pylist() == table["value_type"].to_pylist()

    # The physical record order is represented by monotonically increasing
    # source record offsets and must survive a simple single-file round trip.
    offsets = restored["record_offset"].to_pylist()
    assert offsets == sorted(offsets)

    # Arrow schema metadata should remain available through Parquet's
    # serialized Arrow schema when store_schema is enabled (the default).
    metadata = restored.schema.metadata or {}
    assert metadata.get(b"intouch_lgh.record_order", b"").startswith(b"LGH physical")


def test_pandas_consumes_arrow_table_without_losing_raw_uint64_columns():
    pd = pytest.importorskip("pandas")
    table = _fixture_table()
    frame = table.to_pandas()

    assert len(frame) == table.num_rows
    assert frame["filetime_ticks"].tolist() == table["filetime_ticks"].to_pylist()
    assert frame["value_bits"].tolist() == table["value_bits"].to_pylist()
    assert frame["record_offset"].tolist() == table["record_offset"].to_pylist()
    assert frame["tag"].astype(str).tolist() == table["tag"].to_pylist()


def test_polars_consumes_arrow_table_and_roundtrips_raw_uint64_columns():
    pl = pytest.importorskip("polars")
    table = _fixture_table()
    frame = pl.from_arrow(table)
    restored = frame.to_arrow()

    assert frame.height == table.num_rows
    assert restored["filetime_ticks"].cast(pa.uint64()).to_pylist() == table["filetime_ticks"].to_pylist()
    assert restored["value_bits"].cast(pa.uint64()).to_pylist() == table["value_bits"].to_pylist()
    assert restored["record_offset"].cast(pa.uint64()).to_pylist() == table["record_offset"].to_pylist()
    assert frame["tag"].cast(pl.String).to_list() == table["tag"].to_pylist()


def test_duckdb_consumes_arrow_table_and_preserves_order_when_ordered_by_offset():
    duckdb = pytest.importorskip("duckdb")
    table = _fixture_table()
    con = duckdb.connect()
    try:
        con.register("lgh_arrow", table)
        restored = con.execute("""
            SELECT filetime_ticks, value_bits, record_offset
            FROM lgh_arrow
            ORDER BY record_offset
        """).to_arrow_table()
    finally:
        con.close()

    assert restored["filetime_ticks"].to_pylist() == table["filetime_ticks"].to_pylist()
    assert restored["value_bits"].to_pylist() == table["value_bits"].to_pylist()
    assert restored["record_offset"].to_pylist() == table["record_offset"].to_pylist()
