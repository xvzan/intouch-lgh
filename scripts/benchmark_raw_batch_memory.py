"""Optional raw-batch memory benchmark against a static LGH/IDX pair.

Requires psutil for RSS sampling. Run from the source tree:
    python -m pip install psutil
    PYTHONPATH=. python scripts/benchmark_raw_batch_memory.py path/to/example.lgh

RSS sampling occurs after batches have been emitted; the reported RSS delta is
not an exact peak and also includes demand-paged mmap reads. Python's
tracemalloc peak includes tracked Python allocations, not all native memory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter
import tracemalloc

from intouch_lgh import Archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lgh", type=Path, help="Path to immutable .lgh archive")
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[1024, 8192, 65536])
    parser.add_argument("--sample-every", type=int, default=50)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.lgh.exists() or not args.lgh.with_suffix(".idx").exists():
        parser.error("both the .lgh and corresponding .idx files are required")
    if not args.batch_sizes or any(size <= 0 for size in args.batch_sizes):
        parser.error("batch sizes must be positive")
    if args.sample_every <= 0:
        parser.error("sample-every must be positive")
    try:
        import psutil
    except ImportError as exc:
        raise SystemExit("This optional benchmark requires: pip install psutil") from exc

    process = psutil.Process()
    results = []
    for batch_size in args.batch_sizes:
        with Archive.open(args.lgh, args.lgh.with_suffix(".idx"), validate="none") as archive:
            base_rss = process.memory_info().rss
            peak_sampled_rss = base_rss
            rows = count = largest = 0
            tracemalloc.start()
            started = perf_counter()
            try:
                for batch in archive.iter_batches(batch_size=batch_size):
                    count += 1
                    rows += len(batch)
                    largest = max(largest, len(batch))
                    if count % args.sample_every == 0:
                        peak_sampled_rss = max(peak_sampled_rss, process.memory_info().rss)
                elapsed = perf_counter() - started
                _, allocated_peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            peak_sampled_rss = max(peak_sampled_rss, process.memory_info().rss)
            results.append({
                "batch_size": batch_size,
                "records": rows,
                "batches": count,
                "max_batch": largest,
                "wall_seconds": round(elapsed, 3),
                "peak_python_allocated_bytes": allocated_peak,
                "sampled_rss_delta_bytes": max(0, peak_sampled_rss - base_rss),
            })
    output = json.dumps(results, indent=2, ensure_ascii=False)
    print(output)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
