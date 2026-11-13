from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import threading
import time
from pathlib import Path

VERIFY_COLUMNS = (
    "tag", "value_type", "filetime_ticks", "value_bits",
    "block_offset", "record_index", "record_offset",
)
STRING_COLUMNS = {"tag", "value_type"}


def _json_print(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


class PeakRSS:
    """Sample current process RSS while a single export is running."""
    def __init__(self, psutil, interval=0.005):
        self.psutil = psutil
        self.process = psutil.Process(os.getpid())
        self.interval = interval
        self.baseline = self.process.memory_info().rss
        self.peak = self.baseline
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        while not self._stop.is_set():
            try:
                self.peak = max(self.peak, self.process.memory_info().rss)
            except self.psutil.Error:
                pass
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()
        try:
            self.peak = max(self.peak, self.process.memory_info().rss)
        except self.psutil.Error:
            pass

    @property
    def peak_delta_bytes(self):
        return max(0, self.peak - self.baseline)


def _new_hashes():
    return {name: hashlib.sha256() for name in VERIFY_COLUMNS}


def _update_hashes(batch, hashes, pa):
    for name in VERIFY_COLUMNS:
        col = batch.column(batch.schema.get_field_index(name))
        if name in STRING_COLUMNS:
            if pa.types.is_dictionary(col.type):
                col = col.cast(pa.string())
            for value in col.to_pylist():
                data = value.encode("utf-8")
                hashes[name].update(len(data).to_bytes(4, "little"))
                hashes[name].update(data)
        else:
            hashes[name].update(col.to_numpy(zero_copy_only=False).tobytes())


def _hash_source(archive, batch_size, pa):
    from intouch_lgh.arrow_adapter import iter_arrow_batches

    hashes = _new_hashes()
    rows = 0
    for batch in iter_arrow_batches(
        archive,
        batch_size=batch_size,
        columns=VERIFY_COLUMNS,
        source_metadata="basename",
    ):
        _update_hashes(batch, hashes, pa)
        rows += batch.num_rows
    return rows, {key: value.hexdigest() for key, value in hashes.items()}


def _verify_parquet(path, batch_size, pa, pq, expected_source_basename):
    parquet = pq.ParquetFile(path)
    hashes = _new_hashes()
    rows = 0
    previous_offset = None

    for batch in parquet.iter_batches(
        batch_size=batch_size,
        columns=list(VERIFY_COLUMNS),
    ):
        offsets = batch.column(batch.schema.get_field_index("record_offset")).to_numpy()
        if len(offsets):
            if previous_offset is not None and int(offsets[0]) <= previous_offset:
                raise RuntimeError("record_offset is not strictly increasing between batches")
            if len(offsets) > 1 and (offsets[1:] <= offsets[:-1]).any():
                raise RuntimeError("record_offset is not strictly increasing within a batch")
            previous_offset = int(offsets[-1])
        _update_hashes(batch, hashes, pa)
        rows += batch.num_rows

    if parquet.metadata.num_rows != rows:
        raise RuntimeError(
            f"Parquet footer row count mismatch: {parquet.metadata.num_rows} != {rows}"
        )
    metadata = parquet.schema_arrow.metadata or {}
    actual = metadata.get(b"intouch_lgh.source_file")
    expected = expected_source_basename.encode("utf-8")
    if actual != expected:
        raise RuntimeError(
            f"source_file metadata mismatch: expected {expected!r}, got {actual!r}"
        )
    return rows, {key: value.hexdigest() for key, value in hashes.items()}, parquet.metadata


def export_worker(args):
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        import psutil
    except ImportError as exc:
        raise SystemExit(f"Missing benchmark dependency: {exc}. Install .[interop,benchmark]")

    from intouch_lgh import Archive
    from intouch_lgh.parquet_adapter import export_parquet

    lgh = args.lgh.resolve()
    idx = lgh.with_suffix(".idx")
    output = args.output.resolve()
    if not lgh.is_file() or not idx.is_file():
        raise FileNotFoundError(f"LGH/IDX pair not found: {lgh}, {idx}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)

    with Archive.open(lgh, idx, validate="full") as archive:
        expected_rows = archive.validation_report.numeric_records
        source_rows, source_hashes = _hash_source(
            archive, args.verify_batch_size, pa
        )
        if source_rows != expected_rows:
            raise RuntimeError(
                f"source record count mismatch: iterator={source_rows}, audit={expected_rows}"
            )

        # The worker is a fresh process for each repetition. Baseline is
        # measured after imports, archive validation and source hashing, just
        # before export, so delta is primarily export working-set growth.
        cpu_before = time.process_time()
        with PeakRSS(psutil, interval=args.rss_interval_ms / 1000.0) as memory:
            started = time.perf_counter()
            result = export_parquet(
                archive,
                output,
                batch_size=args.batch_size,
                row_group_size=args.row_group_size,
                compression=None if args.compression == "none" else args.compression,
                source_metadata="basename",
            )
            export_seconds = time.perf_counter() - started
        cpu_seconds = time.process_time() - cpu_before

        rows, exported_hashes, metadata = _verify_parquet(
            output,
            args.verify_batch_size,
            pa,
            pq,
            expected_source_basename=lgh.name,
        )
        row_group_rows = [
            metadata.row_group(i).num_rows
            for i in range(metadata.num_row_groups)
        ]
        if any(value > args.row_group_size for value in row_group_rows):
            raise RuntimeError(
                f"Parquet Row Group exceeds requested size {args.row_group_size}: "
                f"{max(row_group_rows)}"
            )
        if row_group_rows and any(
            value != args.row_group_size for value in row_group_rows[:-1]
        ):
            raise RuntimeError(
                "Non-final Parquet Row Groups do not match the requested size: "
                f"{row_group_rows}"
            )
        mismatches = [
            name for name in VERIFY_COLUMNS
            if exported_hashes[name] != source_hashes[name]
        ]
        if rows != expected_rows or result.rows_written != expected_rows or mismatches:
            raise RuntimeError(
                f"round-trip verification failed: rows={rows}/{expected_rows}, "
                f"mismatched_columns={mismatches}"
            )

        _json_print({
            "status": "ok",
            "python": platform.python_version(),
            "pyarrow": pa.__version__,
            "psutil": psutil.__version__,
            "source_file": lgh.name,
            "rows": rows,
            "batch_size": args.batch_size,
            "row_group_size": args.row_group_size,
            "compression": args.compression,
            "export_seconds": export_seconds,
            "cpu_seconds": cpu_seconds,
            "cpu_utilization_percent": cpu_seconds / export_seconds * 100
                if export_seconds else None,
            "baseline_rss_bytes": memory.baseline,
            "peak_rss_bytes": memory.peak,
            "peak_rss_delta_bytes": memory.peak_delta_bytes,
            "rss_sample_interval_ms": args.rss_interval_ms,
            "file_size_bytes": output.stat().st_size,
            "row_groups": metadata.num_row_groups,
            "row_group_rows_min": min(row_group_rows) if row_group_rows else 0,
            "row_group_rows_max": max(row_group_rows) if row_group_rows else 0,
            "row_group_rows_last": row_group_rows[-1] if row_group_rows else 0,
            "raw_columns_verified": True,
            "raw_column_hashes": exported_hashes,
            "output_file": str(output),
        })


def _duckdb_query(path, repeats):
    import duckdb

    connection = duckdb.connect()
    path_arg = str(path)

    # Discover a representative high-frequency Tag and central time window.
    min_ticks, max_ticks = connection.execute(
        "SELECT min(filetime_ticks), max(filetime_ticks) FROM read_parquet(?)",
        [path_arg],
    ).fetchone()
    common_tag = connection.execute(
        "SELECT tag FROM read_parquet(?) GROUP BY tag "
        "ORDER BY count(*) DESC, tag LIMIT 1",
        [path_arg],
    ).fetchone()[0]
    low = int(min_ticks + (max_ticks - min_ticks) * 0.45)
    high = int(min_ticks + (max_ticks - min_ticks) * 0.55)

    def materialize(sql, params):
        cursor = connection.execute(sql, params)
        to_arrow = getattr(cursor, "to_arrow_table", None)
        if to_arrow is not None:
            return to_arrow()
        return cursor.fetch_arrow_table()

    def full_aggregate():
        row = connection.execute(
            "SELECT count(*), count(DISTINCT tag), "
            "min(filetime_ticks), max(filetime_ticks) FROM read_parquet(?)",
            [path_arg],
        ).fetchone()
        return {
            "rows": int(row[0]), "distinct_tags": int(row[1]),
            "min_filetime_ticks": int(row[2]), "max_filetime_ticks": int(row[3]),
        }

    def tag_filter():
        table = materialize(
            "SELECT filetime_ticks, value_bits FROM read_parquet(?) WHERE tag = ?",
            [path_arg, common_tag],
        )
        return {"rows": table.num_rows, "columns": table.column_names, "tag": common_tag}

    def time_filter():
        table = materialize(
            "SELECT tag, value_bits FROM read_parquet(?) "
            "WHERE filetime_ticks >= ? AND filetime_ticks < ?",
            [path_arg, low, high],
        )
        return {"rows": table.num_rows, "columns": table.column_names,
                "filetime_low": low, "filetime_high": high}

    def projection():
        table = materialize(
            "SELECT filetime_ticks, value_bits FROM read_parquet(?)",
            [path_arg],
        )
        return {"rows": table.num_rows, "columns": table.column_names}

    return _measure_queries(
        "duckdb",
        {
            "full_scan_aggregate": full_aggregate,
            "tag_filter_projection": tag_filter,
            "filetime_range_projection": time_filter,
            "two_column_full_projection": projection,
        },
        repeats,
        {
            "tag_filter_tag": common_tag,
            "filetime_range": [low, high],
        },
    )


def _polars_query(path, repeats):
    import polars as pl

    scan = pl.scan_parquet(str(path))
    limits = scan.select(
        pl.col("filetime_ticks").min().alias("lo"),
        pl.col("filetime_ticks").max().alias("hi"),
    ).collect().row(0)
    min_ticks, max_ticks = int(limits[0]), int(limits[1])
    common_tag = (
        scan.group_by("tag")
        .len()
        .sort(["len", "tag"], descending=[True, False])
        .limit(1)
        .collect()
        .item(0, "tag")
    )
    low = int(min_ticks + (max_ticks - min_ticks) * 0.45)
    high = int(min_ticks + (max_ticks - min_ticks) * 0.55)

    def full_aggregate():
        row = scan.select(
            pl.len().alias("rows"),
            pl.col("tag").n_unique().alias("distinct_tags"),
            pl.col("filetime_ticks").min().alias("min_filetime_ticks"),
            pl.col("filetime_ticks").max().alias("max_filetime_ticks"),
        ).collect().row(0, named=True)
        return {key: int(value) for key, value in row.items()}

    def tag_filter():
        frame = (
            scan.filter(pl.col("tag") == common_tag)
            .select("filetime_ticks", "value_bits")
            .collect()
        )
        return {"rows": frame.height, "columns": frame.columns, "tag": common_tag}

    def time_filter():
        frame = (
            scan.filter(
                (pl.col("filetime_ticks") >= low)
                & (pl.col("filetime_ticks") < high)
            )
            .select("tag", "value_bits")
            .collect()
        )
        return {"rows": frame.height, "columns": frame.columns,
                "filetime_low": low, "filetime_high": high}

    def projection():
        frame = scan.select("filetime_ticks", "value_bits").collect()
        return {"rows": frame.height, "columns": frame.columns}

    return _measure_queries(
        "polars",
        {
            "full_scan_aggregate": full_aggregate,
            "tag_filter_projection": tag_filter,
            "filetime_range_projection": time_filter,
            "two_column_full_projection": projection,
        },
        repeats,
        {
            "tag_filter_tag": common_tag,
            "filetime_range": [low, high],
        },
    )


def _measure_queries(engine, queries, repeats, parameters):
    measured = {}
    for name, query in queries.items():
        warmup_started = time.perf_counter()
        expected = query()
        warmup_seconds = time.perf_counter() - warmup_started
        samples = []
        for _ in range(repeats):
            started = time.perf_counter()
            result = query()
            elapsed = time.perf_counter() - started
            if result != expected:
                raise RuntimeError(
                    f"{engine} query {name} returned inconsistent results: "
                    f"{result!r} != {expected!r}"
                )
            samples.append(elapsed)
        ordered = sorted(samples)
        median = ordered[len(ordered) // 2] if len(ordered) % 2 else (
            ordered[len(ordered)//2 - 1] + ordered[len(ordered)//2]
        ) / 2
        measured[name] = {
            "warmup_seconds": warmup_seconds,
            "samples_seconds": samples,
            "median_seconds": median,
            "min_seconds": min(samples),
            "max_seconds": max(samples),
            "result": expected,
        }
    return {
        "engine": engine,
        "warm_cache_repeats": repeats,
        "parameters": parameters,
        "queries": measured,
    }


def query_worker(args):
    path = args.parquet.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        if args.engine == "duckdb":
            import duckdb
            result = _duckdb_query(path, args.repeats)
            result["version"] = duckdb.__version__
        else:
            import polars as pl
            result = _polars_query(path, args.repeats)
            result["version"] = pl.__version__
    except ImportError as exc:
        _json_print({"status": "skipped", "engine": args.engine, "reason": str(exc)})
        return
    result.update({"status": "ok", "parquet_file": path.name})
    _json_print(result)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export")
    export.add_argument("--lgh", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--batch-size", type=int, required=True)
    export.add_argument("--row-group-size", type=int, required=True)
    export.add_argument("--compression", choices=["zstd", "snappy", "none"], required=True)
    export.add_argument("--verify-batch-size", type=int, default=65536)
    export.add_argument("--rss-interval-ms", type=float, default=5.0)

    query = sub.add_parser("query")
    query.add_argument("--parquet", type=Path, required=True)
    query.add_argument("--engine", choices=["duckdb", "polars"], required=True)
    query.add_argument("--repeats", type=int, default=5)

    args = parser.parse_args()
    if args.command == "export":
        if min(args.batch_size, args.row_group_size, args.verify_batch_size) <= 0:
            parser.error("batch sizes must be positive")
        if args.rss_interval_ms <= 0:
            parser.error("RSS sampling interval must be positive")
        export_worker(args)
    else:
        if args.repeats < 1:
            parser.error("query repeats must be >= 1")
        query_worker(args)


if __name__ == "__main__":
    main()
