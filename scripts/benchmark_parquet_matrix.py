from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
import threading
import time
from pathlib import Path

import psutil

from intouch_lgh import Archive
from intouch_lgh.arrow_adapter import iter_arrow_batches
from intouch_lgh.parquet_adapter import export_parquet

VERIFY_COLUMNS = (
    "tag", "value_type", "filetime_ticks", "value_bits",
    "block_offset", "record_index", "record_offset",
)
STRING_COLUMNS = {"tag", "value_type"}


class PeakRSS:
    """Sample current process RSS during one operation."""
    def __init__(self, interval=0.01):
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
            except psutil.Error:
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
        except psutil.Error:
            pass

    @property
    def peak_delta_bytes(self):
        return max(0, self.peak - self.baseline)


def _pa():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit('Install PyArrow: python -m pip install "intouch-lgh[parquet]"') from exc
    return pa, pq


def _new_hashes():
    return {name: hashlib.sha256() for name in VERIFY_COLUMNS}


def _update_hashes(batch, hashes, pa):
    for name in VERIFY_COLUMNS:
        col = batch.column(batch.schema.get_field_index(name))
        if name in STRING_COLUMNS:
            if pa.types.is_dictionary(col.type):
                col = col.cast(pa.string())
            for value in col.to_pylist():
                b = value.encode("utf-8")
                hashes[name].update(len(b).to_bytes(4, "little"))
                hashes[name].update(b)
        else:
            hashes[name].update(col.to_numpy(zero_copy_only=False).tobytes())


def _hash_source(archive, batch_size, pa):
    hashes = _new_hashes()
    rows = 0
    for batch in iter_arrow_batches(
        archive, batch_size=batch_size, columns=VERIFY_COLUMNS,
        source_metadata="basename",
    ):
        _update_hashes(batch, hashes, pa)
        rows += batch.num_rows
    return rows, {k: v.hexdigest() for k, v in hashes.items()}


def _verify_parquet(path, batch_size, pa, pq, expected_source_basename):
    pf = pq.ParquetFile(path)
    hashes = _new_hashes()
    rows = 0
    previous = None
    for batch in pf.iter_batches(batch_size=batch_size, columns=list(VERIFY_COLUMNS)):
        offsets = batch.column(batch.schema.get_field_index("record_offset")).to_numpy()
        if len(offsets):
            if previous is not None and int(offsets[0]) <= previous:
                raise RuntimeError("record_offset order is not strictly increasing")
            if len(offsets) > 1 and (offsets[1:] <= offsets[:-1]).any():
                raise RuntimeError("record_offset order is not strictly increasing within batch")
            previous = int(offsets[-1])
        _update_hashes(batch, hashes, pa)
        rows += batch.num_rows
    if pf.metadata.num_rows != rows:
        raise RuntimeError("Parquet footer row count mismatch")
    meta = pf.schema_arrow.metadata or {}
    actual_source = meta.get(b"intouch_lgh.source_file")
    expected_source = expected_source_basename.encode("utf-8")
    if actual_source != expected_source:
        raise RuntimeError(
            "source_file metadata mismatch: "
            f"expected basename {expected_source_basename!r}, "
            f"got {actual_source!r}"
        )
    return rows, {k: v.hexdigest() for k, v in hashes.items()}, pf.metadata


def _query_downstream(path):
    result = {}
    try:
        import duckdb
        started = time.perf_counter()
        row = duckdb.connect().execute(
            "SELECT count(*), count(DISTINCT tag), min(filetime_ticks), max(filetime_ticks) "
            "FROM read_parquet(?)", [str(path)]
        ).fetchone()
        result["duckdb"] = {
            "seconds": round(time.perf_counter() - started, 4),
            "rows": row[0], "distinct_tags": row[1],
            "min_filetime_ticks": row[2], "max_filetime_ticks": row[3],
        }
    except ImportError:
        result["duckdb"] = {"skipped": "duckdb not installed"}

    try:
        import polars as pl
        started = time.perf_counter()
        row = (
            pl.scan_parquet(path)
            .select(
                pl.len().alias("rows"),
                pl.col("tag").n_unique().alias("distinct_tags"),
                pl.col("filetime_ticks").min().alias("min_filetime_ticks"),
                pl.col("filetime_ticks").max().alias("max_filetime_ticks"),
            )
            .collect()
            .row(0, named=True)
        )
        result["polars"] = {
            "seconds": round(time.perf_counter() - started, 4),
            **row,
        }
    except ImportError:
        result["polars"] = {"skipped": "polars not installed"}
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark Parquet compression, batch/row-group sizes and peak RSS."
    )
    parser.add_argument("lgh", type=Path, help="Largest LGH file; matching IDX beside it.")
    parser.add_argument("--output-dir", type=Path, default=Path("parquet-matrix"))
    parser.add_argument("--batch-sizes", nargs="+", type=int, default=[16384, 65536, 262144])
    parser.add_argument("--row-group-sizes", nargs="+", type=int, default=[16384, 65536, 262144])
    parser.add_argument("--compressions", nargs="+", choices=["zstd", "snappy", "none"],
                        default=["zstd", "snappy", "none"])
    parser.add_argument("--verify-batch-size", type=int, default=65536)
    parser.add_argument("--keep-files", action="store_true")
    args = parser.parse_args()

    pa, pq = _pa()
    lgh = args.lgh.resolve()
    idx = lgh.with_suffix(".idx")
    if not lgh.is_file() or not idx.is_file():
        parser.error(f"LGH/IDX pair not found: {lgh}, {idx}")
    if any(x <= 0 for x in args.batch_sizes + args.row_group_sizes):
        parser.error("all batch and row-group sizes must be positive")

    # A row group larger than the input batch cannot be filled by the current
    # streaming writer (it does not buffer across RawBatch boundaries), so
    # only meaningful row_group_size <= batch_size pairs are benchmarked.
    configs = [
        (batch, rg, comp)
        for batch in args.batch_sizes
        for rg in args.row_group_sizes
        if rg <= batch
        for comp in args.compressions
    ]
    if not configs:
        parser.error("no valid combinations: each row-group size must be <= batch size")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "python": sys.version,
        "pyarrow": pa.__version__,
        "psutil": psutil.__version__,
        "source_lgh": lgh.name,
        "source_metadata_policy": "basename",
        "benchmark_scope": "export + streaming read-back verification; peak RSS sampled every 10ms",
        "configurations": [],
    }

    with Archive.open(lgh, idx, validate="full") as archive:
        expected_rows = archive.validation_report.numeric_records
        source_rows, source_hashes = _hash_source(archive, args.verify_batch_size, pa)
        if source_rows != expected_rows:
            raise RuntimeError(f"source row count mismatch: audit={expected_rows}, iter={source_rows}")

        for number, (batch_size, row_group_size, compression_name) in enumerate(configs, 1):
            compression = None if compression_name == "none" else compression_name
            output = args.output_dir / (
                f"{lgh.stem}-b{batch_size}-rg{row_group_size}-{compression_name}.parquet"
            )
            output.unlink(missing_ok=True)
            print(f"[{number}/{len(configs)}] batch={batch_size} row_group={row_group_size} "
                  f"compression={compression_name}", flush=True)

            process = psutil.Process(os.getpid())
            cpu_before = time.process_time()
            with PeakRSS() as memory:
                started = time.perf_counter()
                exported = export_parquet(
                    archive, output,
                    batch_size=batch_size,
                    row_group_size=row_group_size,
                    compression=compression,
                    source_metadata="basename",
                )
                export_seconds = time.perf_counter() - started
            cpu_seconds = time.process_time() - cpu_before

            verify_started = time.perf_counter()
            rows, hashes, metadata = _verify_parquet(
                output,
                args.verify_batch_size,
                pa,
                pq,
                expected_source_basename=lgh.name,
            )
            verify_seconds = time.perf_counter() - verify_started
            mismatches = [k for k in VERIFY_COLUMNS if hashes[k] != source_hashes[k]]
            if rows != expected_rows or exported.rows_written != expected_rows or mismatches:
                raise RuntimeError(
                    f"verification failed: rows={rows}/{expected_rows}, mismatches={mismatches}"
                )

            item = {
                "batch_size": batch_size,
                "row_group_size": row_group_size,
                "compression": compression_name,
                "rows": rows,
                "export_seconds": round(export_seconds, 4),
                "verify_seconds": round(verify_seconds, 4),
                "cpu_seconds": round(cpu_seconds, 4),
                "cpu_utilization_percent": round(
                    cpu_seconds / export_seconds * 100, 1
                ) if export_seconds else None,
                "peak_rss_bytes": memory.peak,
                "baseline_rss_bytes": memory.baseline,
                "peak_rss_delta_bytes": memory.peak_delta_bytes,
                "file_size_bytes": output.stat().st_size,
                "rows_per_second": round(rows / export_seconds, 1) if export_seconds else None,
                "row_groups": metadata.num_row_groups,
                "raw_columns_verified": True,
                "output_file": output.name if args.keep_files else None,
            }
            report["configurations"].append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)

            # Use the previous default configuration for downstream query
            # timings when present; otherwise use the first valid matrix item.
            baseline_config = (65536, 65536, "zstd")
            selected_for_query = (
                (batch_size, row_group_size, compression_name) == baseline_config
                or (baseline_config not in configs and number == 1)
            )
            if selected_for_query:
                report["downstream_query_config"] = {
                    "batch_size": batch_size,
                    "row_group_size": row_group_size,
                    "compression": compression_name,
                }
                report["downstream_queries"] = _query_downstream(output)

            if not args.keep_files:
                output.unlink(missing_ok=True)

    report["best_throughput"] = max(
        report["configurations"], key=lambda x: x["rows_per_second"]
    )
    report["smallest_file"] = min(
        report["configurations"], key=lambda x: x["file_size_bytes"]
    )
    report_path = args.output_dir / "parquet_matrix_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport written to: {report_path.resolve()}")
    print(json.dumps({
        "best_throughput": report["best_throughput"],
        "smallest_file": report["smallest_file"],
        "downstream_queries": report.get("downstream_queries"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
