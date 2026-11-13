from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run_one(benchmark_script: Path, lgh: Path, out_dir: Path, repeats: int,
            batch_size: int, row_group_size: int, rss_interval_ms: float) -> dict:
    command = [
        sys.executable, str(benchmark_script), str(lgh),
        "--output-dir", str(out_dir),
        "--batch-sizes", str(batch_size),
        "--row-group-sizes", str(row_group_size),
        "--repeats", str(repeats),
        "--rss-interval-ms", str(rss_interval_ms),
        "--export-only",
    ]
    print(f"\n=== {lgh.name}: {repeats} exports ===", flush=True)
    completed = subprocess.run(command, text=True, encoding="utf-8",
                               errors="replace")
    if completed.returncode:
        raise RuntimeError(f"Benchmark failed for {lgh}: exit={completed.returncode}")
    report_path = out_dir / "parquet_validation_report.json"
    if not report_path.is_file():
        raise RuntimeError(f"Missing report: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if len(report.get("export_layouts", [])) != 1:
        raise RuntimeError(f"Expected exactly one export layout for {lgh.name}")
    layout = report["export_layouts"][0]
    if not layout.get("raw_columns_verified"):
        raise RuntimeError(f"Raw-column verification failed for {lgh.name}")
    return {
        "source_lgh": lgh.name,
        "source_idx": lgh.with_suffix(".idx").name,
        "source_lgh_bytes": lgh.stat().st_size,
        "source_idx_bytes": lgh.with_suffix(".idx").stat().st_size,
        "rows": layout["rows"],
        "numeric_records": layout["rows"],
        "batch_size": layout["batch_size"],
        "row_group_size": layout["row_group_size"],
        "compression": layout["compression"],
        "repeat_count": layout["repeat_count"],
        "export_seconds_median": layout["export_seconds_median"],
        "export_seconds_min": layout["export_seconds_min"],
        "export_seconds_max": layout["export_seconds_max"],
        "rows_per_second_median": layout["rows_per_second_median"],
        "peak_rss_delta_bytes_median": layout["peak_rss_delta_bytes_median"],
        "peak_rss_delta_bytes_min": layout["peak_rss_delta_bytes_min"],
        "peak_rss_delta_bytes_max": layout["peak_rss_delta_bytes_max"],
        "file_size_bytes": layout["file_size_bytes"],
        "row_groups": layout["row_groups"],
        "row_group_rows_min": layout["row_group_rows_min"],
        "row_group_rows_max": layout["row_group_rows_max"],
        "row_group_rows_last": layout["row_group_rows_last"],
        "raw_columns_verified": layout["raw_columns_verified"],
        "raw_column_hashes": layout["raw_column_hashes"],
        "individual_report": str(report_path),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run fixed-configuration Parquet validation across multiple LGH/IDX pairs."
    )
    parser.add_argument("lgh", nargs="+", type=Path,
                        help="One or more LGH files; each matching IDX must be beside it.")
    parser.add_argument("--output-dir", type=Path, default=Path("cross-file-validation"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16384)
    parser.add_argument("--row-group-size", type=int, default=65536)
    parser.add_argument("--rss-interval-ms", type=float, default=5.0)
    args = parser.parse_args()

    if args.repeats < 2 or args.batch_size <= 0 or args.row_group_size <= 0:
        parser.error("repeats must be >= 2 and sizes must be positive")
    sources = [p.expanduser().resolve() for p in args.lgh]
    seen = set()
    for lgh in sources:
        idx = lgh.with_suffix(".idx")
        if not lgh.is_file() or not idx.is_file():
            parser.error(f"LGH/IDX pair not found: {lgh}, {idx}")
        if lgh.name.lower() in seen:
            parser.error(f"Duplicate LGH basename: {lgh.name}")
        seen.add(lgh.name.lower())

    root = args.output_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    benchmark_script = Path(__file__).with_name("benchmark_parquet_validation.py").resolve()
    results = []
    for lgh in sources:
        results.append(run_one(
            benchmark_script, lgh, root / lgh.stem, args.repeats,
            args.batch_size, args.row_group_size, args.rss_interval_ms
        ))

    report = {
        "benchmark": "intouch-lgh v1.0.0a10 cross-file validation",
        "settings": {
            "batch_size": args.batch_size,
            "row_group_size": args.row_group_size,
            "compression": "zstd",
            "repeats_per_file": args.repeats,
            "rss_sampling_interval_ms": args.rss_interval_ms,
            "worker_process_isolation": True,
        },
        "files_tested": len(results),
        "all_raw_columns_verified": all(r["raw_columns_verified"] for r in results),
        "results": results,
    }
    report_path = root / "cross_file_validation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nCross-file report written to: {report_path}", flush=True)
    print(json.dumps({
        "files_tested": len(results),
        "rows_total": sum(r["rows"] for r in results),
        "all_raw_columns_verified": report["all_raw_columns_verified"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
