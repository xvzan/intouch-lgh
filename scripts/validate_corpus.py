from __future__ import annotations

import argparse
import json
import math
import struct
import os
import sys
import time
from collections import Counter
from pathlib import Path

from intouch_lgh import Archive, __version__
from intouch_lgh.index import IDXReader, inspect_idx_topology


def validate_pair(lgh: Path, idx: Path, batch_size: int) -> dict:
    started = time.perf_counter()
    topology = inspect_idx_topology(idx)
    if not topology["matches_observed_layout"]:
        raise RuntimeError(f"{idx.name}: IDX topology failed: {topology['issues']}")

    type_rows = Counter()
    rows = 0
    with Archive.open(lgh, idx, validate="full") as archive:
        for batch in archive.iter_batches(batch_size=batch_size, validate_types=False):
            rows += len(batch)
            for block in batch.blocks:
                type_rows[block.value_type] += block.row_stop - block.row_start
                # REAL has no additional semantic constraint. Check only the
                # BOOL/INT payloads instead of constructing a Record per row.
                if block.value_type in ("BOOL", "INT"):
                    for row in range(block.row_start, block.row_stop):
                        value = struct.unpack_from("<d", batch.value_bits, row * 8)[0]
                        if not math.isfinite(value) or not value.is_integer():
                            raise RuntimeError(
                                f"{lgh.name}: invalid {block.value_type} payload "
                                f"at source block {block.block_number}, row {row}"
                            )
                        if block.value_type == "BOOL" and value not in (0.0, 1.0):
                            raise RuntimeError(
                                f"{lgh.name}: invalid BOOL payload {value!r} "
                                f"at source block {block.block_number}, row {row}"
                            )

    if rows != sum(type_rows.values()):
        raise RuntimeError(f"{lgh.name}: row/type count mismatch")

    reader = IDXReader(idx)
    scan = {}
    for entry in reader.entries():
        scan.setdefault(entry.tag, []).append(entry.block_number)
    for tag, expected in scan.items():
        actual = reader.blocks_for_tag(tag, method="tree")
        if actual != sorted(expected):
            raise RuntimeError(f"{idx.name}: tree/scan mismatch for Tag {tag!r}")

    return {
        "sample": lgh.stem,
        "lgh_bytes": lgh.stat().st_size,
        "idx_bytes": idx.stat().st_size,
        "rows": rows,
        "type_rows": dict(sorted(type_rows.items())),
        "tag_count": len(scan),
        "idx_page_count": topology["page_count"],
        "idx_internal_nodes": topology["internal_node_count"],
        "idx_leaf_nodes": topology["leaf_count"],
        "idx_depth": topology["max_depth"],
        "idx_topology": "passed",
        "strict_type_validation": "passed",
        "tree_vs_scan": "passed",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Full LGH/IDX compatibility validation over an external sample corpus."
    )
    parser.add_argument("corpus_dir", type=Path,
                        help="Directory containing matching <stem>.lgh and <stem>.idx files")
    parser.add_argument("--samples", nargs="*", default=None,
                        help="Archive stems; default: discover complete LGH/IDX pairs")
    parser.add_argument("--batch-size", type=int, default=65536)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Report path; default: <corpus_dir>/corpus_validation_report.json"
        ),
    )
    args = parser.parse_args()

    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    root = args.corpus_dir.expanduser().resolve()
    samples = args.samples if args.samples is not None else [
        p.stem for p in sorted(root.glob("*.lgh")) if p.with_suffix(".idx").is_file()
    ]
    if not samples:
        parser.error("No matching LGH/IDX pairs found")
    results = []
    for stem in samples:
        lgh, idx = root / f"{stem}.lgh", root / f"{stem}.idx"
        if not lgh.is_file() or not idx.is_file():
            raise FileNotFoundError(f"Missing LGH/IDX pair: {lgh}, {idx}")
        print(f"Validating {stem} ...", flush=True)
        results.append(validate_pair(lgh, idx, args.batch_size))
        print(f"  rows={results[-1]['rows']:,}; "
              f"tags={results[-1]['tag_count']}; "
              f"depth={results[-1]['idx_depth']}; "
              f"elapsed={results[-1]['elapsed_seconds']}s", flush=True)

    report = {
        "package_version": __version__,
        "validation": "full LGH/IDX + strict types + IDX topology + tree/scan parity",
        "batch_size": args.batch_size,
        "sample_count": len(results),
        "total_rows": sum(r["rows"] for r in results),
        "all_passed": True,
        "results": results,
    }
    output = (
        args.output.expanduser().resolve()
        if args.output is not None
        else root / "corpus_validation_report.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport: {output}", flush=True)


if __name__ == "__main__":
    main()
