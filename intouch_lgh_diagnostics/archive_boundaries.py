from __future__ import annotations

import argparse
import json
import math
import statistics
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from intouch_lgh import Archive, __version__
from intouch_lgh.reader import FILETIME_MAX_TICKS, FILETIME_UNIX_EPOCH


def format_filetime(ticks: int | None) -> str | None:
    """Format FILETIME without discarding its seventh decimal digit."""
    if ticks is None or not 0 <= ticks <= FILETIME_MAX_TICKS:
        return None
    microseconds, sub_tick = divmod(ticks - FILETIME_UNIX_EPOCH, 10)
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        microseconds=microseconds
    )
    prefix = dt.isoformat(timespec="seconds").removesuffix("+00:00")
    if dt.microsecond or sub_tick:
        return f"{prefix}.{dt.microsecond:06d}{sub_tick}+00:00"
    return prefix + "+00:00"


def summarize_archive(
    lgh_path: Path,
    idx_path: Path,
    *,
    batch_size: int = 65_536,
) -> dict:
    """Summarize per-Tag physical first/last observations using public APIs."""
    tags: dict[str, dict] = {}
    total_records = 0
    total_segments = 0
    global_first = None
    global_last = None

    with Archive.open(lgh_path, idx_path, validate="full") as archive:
        for batch in archive.iter_batches(batch_size=batch_size):
            total_records += len(batch)
            for block in batch.blocks:
                start, stop = block.row_start, block.row_stop
                if stop <= start:
                    continue

                first_ticks = struct.unpack_from(
                    "<Q", batch.filetime_ticks, start * 8
                )[0]
                last_ticks = struct.unpack_from(
                    "<Q", batch.filetime_ticks, (stop - 1) * 8
                )[0]
                first_index = batch.record_indices[start]
                last_index = batch.record_indices[stop - 1]
                count = stop - start

                if global_first is None:
                    global_first = first_ticks
                global_last = last_ticks
                total_segments += 1

                item = tags.get(block.tag)
                if item is None:
                    tags[block.tag] = {
                        "value_type": block.value_type,
                        "value_types": [block.value_type],
                        "record_count": count,
                        "block_count": 1,
                        "first_filetime_ticks": first_ticks,
                        "last_filetime_ticks": last_ticks,
                        "first_block_number": block.block_number,
                        "last_block_number": block.block_number,
                        "first_block_offset": block.block_offset,
                        "last_block_offset": block.block_offset,
                        "first_record_index": first_index,
                        "last_record_index": last_index,
                    }
                    continue

                if block.value_type not in item["value_types"]:
                    item["value_types"].append(block.value_type)
                    item["value_types"].sort()

                item["record_count"] += count
                # A source block can be split between adjacent batches. Count
                # it only once for this Tag.
                if item["last_block_number"] != block.block_number:
                    item["block_count"] += 1

                item["last_filetime_ticks"] = last_ticks
                item["last_block_number"] = block.block_number
                item["last_block_offset"] = block.block_offset
                item["last_record_index"] = last_index

    for item in tags.values():
        item["first_timestamp_utc"] = format_filetime(
            item["first_filetime_ticks"]
        )
        item["last_timestamp_utc"] = format_filetime(
            item["last_filetime_ticks"]
        )

    return {
        "file": lgh_path.name,
        "lgh_bytes": lgh_path.stat().st_size,
        "idx_bytes": idx_path.stat().st_size,
        "numeric_records": total_records,
        "tag_count": len(tags),
        "batch_segments_processed": total_segments,
        "first_physical_filetime_ticks": global_first,
        "last_physical_filetime_ticks": global_last,
        "first_physical_timestamp_utc": format_filetime(global_first),
        "last_physical_timestamp_utc": format_filetime(global_last),
        "tags": tags,
    }



def summarize_forward_deltas(comparisons: list[dict]) -> dict | None:
    """Descriptive interval statistics only; no gap threshold is inferred."""
    values = sorted(
        item["delta_seconds"]
        for item in comparisons
        if item["status"] == "forward"
    )
    if not values:
        return None

    def nearest_rank(percentile: float) -> float:
        index = max(0, math.ceil(percentile * len(values)) - 1)
        return values[index]

    return {
        "count": len(values),
        "min_seconds": values[0],
        "median_seconds": statistics.median(values),
        "p90_seconds": nearest_rank(0.90),
        "max_seconds": values[-1],
    }


def compare_tag_boundaries(
    previous: dict[str, dict],
    current: dict[str, dict],
) -> list[dict]:
    """Compare previous-file last observation with current-file first."""
    comparisons = []
    for tag in sorted(set(previous) | set(current)):
        left = previous.get(tag)
        right = current.get(tag)

        if left is None:
            comparisons.append({
                "tag": tag,
                "status": "appears_in_current_only",
                "previous_value_type": None,
                "current_value_type": right["value_type"],
            })
            continue
        if right is None:
            comparisons.append({
                "tag": tag,
                "status": "missing_from_current",
                "previous_value_type": left["value_type"],
                "current_value_type": None,
            })
            continue

        previous_ticks = left["last_filetime_ticks"]
        current_ticks = right["first_filetime_ticks"]
        delta_ticks = current_ticks - previous_ticks

        if delta_ticks < 0:
            status = "backward_overlap"
        elif delta_ticks == 0:
            status = "equal_timestamp_boundary"
        else:
            status = "forward"

        comparisons.append({
            "tag": tag,
            "status": status,
            "previous_value_type": left["value_type"],
            "current_value_type": right["value_type"],
            "value_type_changed": left["value_type"] != right["value_type"],
            "previous_last_filetime_ticks": previous_ticks,
            "current_first_filetime_ticks": current_ticks,
            "previous_last_timestamp_utc": left["last_timestamp_utc"],
            "current_first_timestamp_utc": right["first_timestamp_utc"],
            "delta_ticks": delta_ticks,
            "delta_seconds": delta_ticks / 10_000_000,
            "previous_last_block_number": left["last_block_number"],
            "current_first_block_number": right["first_block_number"],
            "previous_last_record_index": left["last_record_index"],
            "current_first_record_index": right["first_record_index"],
        })
    return comparisons


def discover_pairs(
    corpus_dir: Path,
    samples: Iterable[str] | None = None,
) -> list[tuple[Path, Path]]:
    root = corpus_dir.expanduser().resolve()
    if samples:
        stems = list(samples)
    else:
        stems = sorted(path.stem for path in root.glob("*.lgh"))

    pairs = []
    for stem in stems:
        lgh_path = root / f"{stem}.lgh"
        idx_path = root / f"{stem}.idx"
        if not lgh_path.is_file():
            raise FileNotFoundError(f"Missing LGH file: {lgh_path}")
        if not idx_path.is_file():
            raise FileNotFoundError(f"Missing matching IDX file: {idx_path}")
        pairs.append((lgh_path, idx_path))
    if len(pairs) < 2:
        raise ValueError("At least two LGH/IDX pairs are required")
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare per-Tag first/last observations across LGH archives. "
            "Files are compared in the supplied order (default: filename order)."
        )
    )
    parser.add_argument("corpus_dir", type=Path)
    parser.add_argument(
        "--samples",
        nargs="*",
        help="Optional ordered list of archive stems; otherwise use filename order.",
    )
    parser.add_argument("--batch-size", type=int, default=65_536)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Report path; default: <corpus_dir>/archive_boundary_report.json",
    )
    args = parser.parse_args()

    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")

    corpus_root = args.corpus_dir.expanduser().resolve()
    pairs = discover_pairs(corpus_root, args.samples)
    summaries = []

    for lgh_path, idx_path in pairs:
        print(f"Summarizing {lgh_path.name} ...", flush=True)
        summary = summarize_archive(
            lgh_path, idx_path, batch_size=args.batch_size
        )
        summaries.append(summary)
        print(
            f"  records={summary['numeric_records']:,}; "
            f"Tags={summary['tag_count']}; "
            f"physical={summary['first_physical_timestamp_utc']} .. "
            f"{summary['last_physical_timestamp_utc']}",
            flush=True,
        )

    pair_reports = []
    for previous, current in zip(summaries, summaries[1:]):
        comparisons = compare_tag_boundaries(
            previous["tags"], current["tags"]
        )
        counts: dict[str, int] = {}
        for item in comparisons:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        type_changes = sum(
            bool(item.get("value_type_changed")) for item in comparisons
        )
        pair_reports.append({
            "previous_file": previous["file"],
            "current_file": current["file"],
            "previous_global_last_timestamp_utc": previous["last_physical_timestamp_utc"],
            "current_global_first_timestamp_utc": current["first_physical_timestamp_utc"],
            "tag_comparison_count": len(comparisons),
            "status_counts": dict(sorted(counts.items())),
            "value_type_change_count": type_changes,
            "forward_delta_seconds_summary": summarize_forward_deltas(comparisons),
            "comparisons": comparisons,
        })

    report = {
        "tool_version": __version__,
        "scope": (
            "Per-Tag physical last observation in each file compared with "
            "per-Tag physical first observation in the next supplied file."
        ),
        "ordering": (
            "Explicit --samples order" if args.samples
            else "Lexicographic filename order; not inferred chronological order."
        ),
        "batch_size": args.batch_size,
        "archive_count": len(summaries),
        "pair_count": len(pair_reports),
        "total_numeric_records": sum(
            item["numeric_records"] for item in summaries
        ),
        "threshold_policy": (
            "No universal gap threshold is applied. Positive deltas are "
            "reported as elapsed time, not classified as data loss."
        ),
        "archives": summaries,
        "adjacent_file_comparisons": pair_reports,
    }

    output = (
        args.output.expanduser().resolve()
        if args.output is not None
        else corpus_root / "archive_boundary_report.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nReport: {output}", flush=True)


if __name__ == "__main__":
    main()
