from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
from pathlib import Path


def _run_worker(command):
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Benchmark worker failed.\n"
            f"Command: {subprocess.list2cmdline(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    try:
        return json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Worker did not return valid JSON.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        ) from exc


def _median(values):
    return statistics.median(values)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark independent Parquet source Batch Size and Row Group Size "
            "settings, then compare DuckDB/Polars query performance."
        )
    )
    parser.add_argument("lgh", type=Path, help="LGH file; matching IDX must be beside it.")
    parser.add_argument("--output-dir", type=Path, default=Path("parquet-validation"))
    parser.add_argument(
        "--batch-sizes", nargs="+", type=int, default=None,
        help="Source RawBatch/Arrow batch sizes (default: 16384 65536).",
    )
    parser.add_argument(
        "--row-group-sizes", nargs="+", type=int, default=None,
        help="Parquet Row Group sizes (default: 16384 65536 262144).",
    )
    # Backward-compatible shorthand from a7: use each supplied size for both
    # dimensions. New experiments should use the independent options above.
    parser.add_argument(
        "--sizes", nargs="+", type=int, default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--repeats", type=int, default=3,
                        help="Independent export worker processes per layout.")
    parser.add_argument("--query-repeats", type=int, default=5,
                        help="Warm-cache repetitions per query and engine.")
    parser.add_argument("--verify-batch-size", type=int, default=65536)
    parser.add_argument("--rss-interval-ms", type=float, default=5.0)
    parser.add_argument("--engines", nargs="+", choices=["duckdb", "polars"],
                        default=["duckdb", "polars"])
    parser.add_argument("--keep-files", action="store_true",
                        help="Keep representative Parquet files after the benchmark.")
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Skip DuckDB/Polars queries; only benchmark and verify Parquet exports.",
    )
    args = parser.parse_args()

    if args.sizes is not None:
        if args.batch_sizes is not None or args.row_group_sizes is not None:
            parser.error("--sizes cannot be combined with --batch-sizes or --row-group-sizes")
        args.batch_sizes = list(args.sizes)
        args.row_group_sizes = list(args.sizes)
    else:
        args.batch_sizes = args.batch_sizes or [16384, 65536]
        args.row_group_sizes = args.row_group_sizes or [16384, 65536, 262144]

    lgh = args.lgh.expanduser().resolve()
    idx = lgh.with_suffix(".idx")
    if not lgh.is_file() or not idx.is_file():
        parser.error(f"LGH/IDX pair not found: {lgh}, {idx}")
    if not args.batch_sizes or any(size <= 0 for size in args.batch_sizes):
        parser.error("--batch-sizes must contain positive integers")
    if not args.row_group_sizes or any(size <= 0 for size in args.row_group_sizes):
        parser.error("--row-group-sizes must contain positive integers")
    if args.repeats < 2:
        parser.error("--repeats must be at least 2 for a useful repeated benchmark")
    if args.query_repeats < 3:
        parser.error("--query-repeats must be at least 3")
    if args.verify_batch_size <= 0 or args.rss_interval_ms <= 0:
        parser.error("verification batch size and RSS interval must be positive")

    # Remove duplicate settings while preserving the caller's order.
    args.batch_sizes = list(dict.fromkeys(args.batch_sizes))
    args.row_group_sizes = list(dict.fromkeys(args.row_group_sizes))

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = output_dir / "artifacts"
    artifact_dir.mkdir(exist_ok=True)
    worker = Path(__file__).with_name("benchmark_parquet_worker.py").resolve()

    report = {
        "benchmark": "intouch-lgh v1.0.0a9 independent Batch/Row Group validation",
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "source_lgh": lgh.name,
        "source_idx": idx.name,
        "export_repeats": args.repeats,
        "query_repeats": args.query_repeats,
        "export_only": args.export_only,
        "rss_sampling_interval_ms": args.rss_interval_ms,
        "export_policy": {
            "compression": "zstd",
            "batch_sizes": args.batch_sizes,
            "row_group_sizes": args.row_group_sizes,
            "batch_size_independent_of_row_group_size": True,
            "row_group_buffering": "accumulate Arrow batches; write full row groups; write one final partial group",
            "source_metadata": "basename",
            "worker_process_isolation": True,
        },
        "export_layouts": [],
        "query_layouts": [],
    }

    total_layouts = len(args.batch_sizes) * len(args.row_group_sizes)
    print("InTouch LGH v1.0.0a8 independent Batch/Row Group benchmark", flush=True)
    print(f"Source: {lgh.name}", flush=True)
    print(f"Export layouts: {total_layouts}; independent repeats per layout: {args.repeats}",
          flush=True)

    representative_files = {}
    query_files_by_row_group = {}

    layout_index = 0
    for batch_size in args.batch_sizes:
        for row_group_size in args.row_group_sizes:
            layout_index += 1
            key = (batch_size, row_group_size)
            print(
                f"\n[{layout_index}/{total_layouts}] "
                f"batch={batch_size:,} row_group={row_group_size:,} compression=zstd",
                flush=True,
            )
            runs = []
            representative = artifact_dir / (
                f"{lgh.stem}-bs{batch_size}-rg{row_group_size}-zstd.parquet"
            )
            representative.unlink(missing_ok=True)

            for repeat in range(1, args.repeats + 1):
                output = representative if repeat == 1 else (
                    artifact_dir / (
                        f".{lgh.stem}-bs{batch_size}-rg{row_group_size}-repeat{repeat}.parquet"
                    )
                )
                output.unlink(missing_ok=True)
                command = [
                    sys.executable, str(worker), "export",
                    "--lgh", str(lgh),
                    "--output", str(output),
                    "--batch-size", str(batch_size),
                    "--row-group-size", str(row_group_size),
                    "--compression", "zstd",
                    "--verify-batch-size", str(args.verify_batch_size),
                    "--rss-interval-ms", str(args.rss_interval_ms),
                ]
                print(f"  export repeat {repeat}/{args.repeats} ...", flush=True)
                result = _run_worker(command)
                runs.append(result)
                print(
                    f"    {result['export_seconds']:.4f}s, "
                    f"{result['rows']/result['export_seconds']:.0f} rows/s, "
                    f"RSS delta {result['peak_rss_delta_bytes']/1024/1024:.2f} MiB, "
                    f"{result['file_size_bytes']/1024/1024:.2f} MiB, "
                    f"{result['row_groups']} row groups",
                    flush=True,
                )
                if repeat > 1:
                    output.unlink(missing_ok=True)

            if any(not run.get("raw_columns_verified") for run in runs):
                raise RuntimeError(
                    f"Raw column verification failed for batch={batch_size}, "
                    f"row_group={row_group_size}"
                )
            hashes = [run["raw_column_hashes"] for run in runs]
            if any(value != hashes[0] for value in hashes[1:]):
                raise RuntimeError(
                    f"Source hashes changed between repeats for batch={batch_size}, "
                    f"row_group={row_group_size}"
                )
            if len({run["file_size_bytes"] for run in runs}) != 1:
                raise RuntimeError(
                    f"Parquet output sizes differed between repeats for "
                    f"batch={batch_size}, row_group={row_group_size}"
                )
            if len({run["row_groups"] for run in runs}) != 1:
                raise RuntimeError(
                    f"Row Group counts differed between repeats for "
                    f"batch={batch_size}, row_group={row_group_size}"
                )

            report["export_layouts"].append({
                "batch_size": batch_size,
                "row_group_size": row_group_size,
                "compression": "zstd",
                "rows": runs[0]["rows"],
                "repeat_count": len(runs),
                "export_seconds_samples": [run["export_seconds"] for run in runs],
                "export_seconds_median": _median([run["export_seconds"] for run in runs]),
                "export_seconds_min": min(run["export_seconds"] for run in runs),
                "export_seconds_max": max(run["export_seconds"] for run in runs),
                "rows_per_second_median": _median([
                    run["rows"] / run["export_seconds"] for run in runs
                ]),
                "cpu_seconds_median": _median([run["cpu_seconds"] for run in runs]),
                "cpu_utilization_percent_median": _median([
                    run["cpu_utilization_percent"] for run in runs
                ]),
                "baseline_rss_bytes_samples": [run["baseline_rss_bytes"] for run in runs],
                "peak_rss_bytes_samples": [run["peak_rss_bytes"] for run in runs],
                "peak_rss_delta_bytes_samples": [run["peak_rss_delta_bytes"] for run in runs],
                "peak_rss_delta_bytes_median": _median([
                    run["peak_rss_delta_bytes"] for run in runs
                ]),
                "peak_rss_delta_bytes_min": min(
                    run["peak_rss_delta_bytes"] for run in runs
                ),
                "peak_rss_delta_bytes_max": max(
                    run["peak_rss_delta_bytes"] for run in runs
                ),
                "file_size_bytes": runs[0]["file_size_bytes"],
                "row_groups": runs[0]["row_groups"],
                "row_group_rows_min": runs[0]["row_group_rows_min"],
                "row_group_rows_max": runs[0]["row_group_rows_max"],
                "row_group_rows_last": runs[0]["row_group_rows_last"],
                "raw_columns_verified": True,
                "raw_column_hashes": hashes[0],
                "runs": runs,
            })
            representative_files[key] = representative
            # Query layout depends on physical Row Group layout, not source
            # batch size. Use the first tested batch size as its representative.
            if row_group_size not in query_files_by_row_group:
                query_files_by_row_group[row_group_size] = {
                    "path": representative,
                    "batch_size": batch_size,
                }

    # Cross-layout parity: changing source Batch Size must not change the
    # decoded records or physical Parquet layout when Row Group Size is fixed.
    reference_by_row_group = {}
    for layout in report["export_layouts"]:
        rg = layout["row_group_size"]
        reference = reference_by_row_group.setdefault(
            rg,
            {
                "rows": layout["rows"],
                "raw_column_hashes": layout["raw_column_hashes"],
                "row_groups": layout["row_groups"],
                "row_group_rows_min": layout["row_group_rows_min"],
                "row_group_rows_max": layout["row_group_rows_max"],
                "row_group_rows_last": layout["row_group_rows_last"],
            },
        )
        for field in (
            "rows", "raw_column_hashes", "row_groups",
            "row_group_rows_min", "row_group_rows_max", "row_group_rows_last",
        ):
            if layout[field] != reference[field]:
                raise RuntimeError(
                    f"Batch Size changed {field} for Row Group {rg}: "
                    f"reference={reference[field]!r}, current={layout[field]!r}"
                )

    # Query once per physical Row Group layout; export Batch Size has no
    # intended effect on the finished Parquet query layout. Focused export-only
    # experiments skip this phase entirely.
    for row_group_size in ([] if args.export_only else args.row_group_sizes):
        selected = query_files_by_row_group[row_group_size]
        parquet = selected["path"]
        layout_result = {
            "row_group_size": row_group_size,
            "query_source_batch_size": selected["batch_size"],
            "compression": "zstd",
            "parquet_file_size_bytes": parquet.stat().st_size,
            "engines": {},
        }
        for engine in args.engines:
            print(f"\nQuery: Row Group {row_group_size:,}, {engine}", flush=True)
            command = [
                sys.executable, str(worker), "query",
                "--parquet", str(parquet),
                "--engine", engine,
                "--repeats", str(args.query_repeats),
            ]
            result = _run_worker(command)
            layout_result["engines"][engine] = result
            if result.get("status") == "ok":
                for query_name, query_result in result["queries"].items():
                    print(
                        f"  {query_name}: median "
                        f"{query_result['median_seconds']:.4f}s "
                        f"({query_result['result']['rows']:,} rows)",
                        flush=True,
                    )
        successful = [
            item for item in layout_result["engines"].values()
            if item.get("status") == "ok"
        ]
        if len(successful) > 1:
            first_engine = successful[0]
            for other in successful[1:]:
                for query_name in first_engine["queries"]:
                    if first_engine["queries"][query_name]["result"] != (
                        other["queries"][query_name]["result"]
                    ):
                        raise RuntimeError(
                            f"Cross-engine query result mismatch at Row Group "
                            f"{row_group_size}: {query_name}"
                        )
        report["query_layouts"].append(layout_result)

    if not args.keep_files:
        for path in representative_files.values():
            path.unlink(missing_ok=True)
        try:
            artifact_dir.rmdir()
        except OSError:
            pass

    best_export = max(
        report["export_layouts"], key=lambda item: item["rows_per_second_median"]
    )
    lowest_rss = min(
        report["export_layouts"], key=lambda item: item["peak_rss_delta_bytes_median"]
    )
    report["summary"] = {
        "fastest_export_layout": {
            "batch_size": best_export["batch_size"],
            "row_group_size": best_export["row_group_size"],
            "median_rows_per_second": best_export["rows_per_second_median"],
        },
        "lowest_export_rss_delta_layout": {
            "batch_size": lowest_rss["batch_size"],
            "row_group_size": lowest_rss["row_group_size"],
            "median_peak_rss_delta_bytes": lowest_rss["peak_rss_delta_bytes_median"],
        },
        "cross_batch_size_parity": "passed",
        "query_benchmark_status": (
            "skipped_export_only" if args.export_only else "completed"
        ),
        "query_cache_semantics": (
            None if args.export_only else
            "One warm-up execution followed by repeated warm-cache timings. "
            "No claim of cold-cache performance."
        ),
    }
    report_path = output_dir / "parquet_validation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport written to: {report_path}", flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
