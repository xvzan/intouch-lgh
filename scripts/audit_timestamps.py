from __future__ import annotations

import argparse
import json
import struct
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from intouch_lgh import __version__
from intouch_lgh.reader import (
    BLOCK_SIZE, ENTRY_SIZE, FILETIME_MAX_TICKS, FILETIME_UNIX_EPOCH,
    HEADER_SIZE, LGHReader, MARKER_OPCODE,
)


def format_filetime(ticks: int) -> str | None:
    if not 0 <= ticks <= FILETIME_MAX_TICKS:
        return None
    microseconds, sub_microsecond_tick = divmod(
        ticks - FILETIME_UNIX_EPOCH, 10
    )
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        microseconds=microseconds
    )
    base = dt.isoformat(timespec="seconds")
    if base.endswith("+00:00"):
        base = base[:-6]
    if dt.microsecond or sub_microsecond_tick:
        return (
            base + f".{dt.microsecond:06d}{sub_microsecond_tick}+00:00"
        )
    return base + "+00:00"


def inspect_markers(path: Path) -> list[dict]:
    markers = []
    with path.open("rb") as stream:
        offset = 0
        while raw := stream.read(BLOCK_SIZE):
            if len(raw) != BLOCK_SIZE:
                break
            if raw[:4] == MARKER_OPCODE:
                ticks = struct.unpack_from("<Q", raw, 8)[0]
                text = raw[16:56].split(bytes([0]), 1)[0].decode(
                    "ascii", errors="replace"
                )
                marker_dt = None
                if 0 <= ticks <= FILETIME_MAX_TICKS:
                    microseconds, _sub_tick = divmod(
                        ticks - FILETIME_UNIX_EPOCH, 10
                    )
                    marker_dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
                        microseconds=microseconds
                    )
                text_matches_utc_minute = False
                if marker_dt is not None and len(text) == 9 and text.startswith("$") and text[1:].isdigit():
                    month, day, hour, minute = (
                        int(text[1:3]), int(text[3:5]),
                        int(text[5:7]), int(text[7:9])
                    )
                    text_matches_utc_minute = (
                        (marker_dt.month, marker_dt.day,
                         marker_dt.hour, marker_dt.minute)
                        == (month, day, hour, minute)
                    )
                markers.append({
                    "offset": offset,
                    "text": text,
                    "header_filetime_ticks": ticks,
                    "header_timestamp_utc": format_filetime(ticks),
                    "text_matches_header_utc_minute": text_matches_utc_minute,
                })
            offset += BLOCK_SIZE
    return markers


def inspect_lgh(path: Path) -> dict:
    reader = LGHReader(path)
    blocks = rows = 0
    types = Counter()
    header_mismatches = 0
    invalid_ticks = zero_ticks = 0
    tick_remainders = Counter()
    within_inversions = within_duplicates = 0
    cross_block_inversions = cross_block_duplicates = 0
    last_tick_by_tag: dict[str, int] = {}
    minimum = maximum = None

    for info, raw in reader._blocks():
        blocks += 1
        rows += info.count
        types[info.value_type] += info.count
        first_tick = struct.unpack_from("<Q", raw, HEADER_SIZE)[0]
        header_mismatches += info.header_filetime != first_tick

        previous = None
        for index, (ticks, _value) in enumerate(
            struct.iter_unpack(
                "<Qd",
                raw[HEADER_SIZE:HEADER_SIZE + info.count * ENTRY_SIZE]
            )
        ):
            if ticks > FILETIME_MAX_TICKS:
                invalid_ticks += 1
            if ticks == 0:
                zero_ticks += 1
            tick_remainders[ticks % 10_000] += 1
            minimum = ticks if minimum is None else min(minimum, ticks)
            maximum = ticks if maximum is None else max(maximum, ticks)
            if previous is not None:
                if ticks < previous:
                    within_inversions += 1
                elif ticks == previous:
                    within_duplicates += 1
            previous = ticks

        last_for_tag = last_tick_by_tag.get(info.tag)
        if last_for_tag is not None:
            if first_tick < last_for_tag:
                cross_block_inversions += 1
            elif first_tick == last_for_tag:
                cross_block_duplicates += 1
        last_tick_by_tag[info.tag] = previous

    return {
        "sample": path.stem,
        "lgh_bytes": path.stat().st_size,
        "numeric_blocks": blocks,
        "numeric_records": rows,
        "record_types": dict(sorted(types.items())),
        "tag_count": len(last_tick_by_tag),
        "header_first_record_mismatches": header_mismatches,
        "zero_timestamps": zero_ticks,
        "out_of_datetime_range_timestamps": invalid_ticks,
        "all_timestamps_on_1ms_grid": set(tick_remainders) == {0},
        "timestamp_remainders_mod_10000": dict(sorted(tick_remainders.items())),
        "min_timestamp_filetime_ticks": minimum,
        "max_timestamp_filetime_ticks": maximum,
        "min_timestamp_utc": format_filetime(minimum) if minimum is not None else None,
        "max_timestamp_utc": format_filetime(maximum) if maximum is not None else None,
        "within_block_time_inversions": within_inversions,
        "within_block_adjacent_equal_timestamps": within_duplicates,
        "cross_block_tag_time_inversions": cross_block_inversions,
        "cross_block_tag_equal_boundaries": cross_block_duplicates,
        "date_markers": inspect_markers(path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit raw LGH FILETIME values and block/Tag chronology."
    )
    parser.add_argument("corpus_dir", type=Path)
    parser.add_argument(
        "--samples", nargs="*", default=None,
        help="Archive stems; default: discover LGH files in the corpus directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Report path; default: <corpus_dir>/timestamp_audit_report.json",
    )
    args = parser.parse_args()

    root = args.corpus_dir.expanduser().resolve()
    samples = args.samples if args.samples is not None else [
        p.stem for p in sorted(root.glob("*.lgh"))
    ]
    if not samples:
        parser.error("No LGH files found")
    results = []
    for stem in samples:
        path = root / f"{stem}.lgh"
        if not path.is_file():
            raise FileNotFoundError(path)
        print(f"Auditing {stem} ...", flush=True)
        result = inspect_lgh(path)
        results.append(result)
        print(
            f"  rows={result['numeric_records']:,}; "
            f"header mismatches={result['header_first_record_mismatches']}; "
            f"within inversions={result['within_block_time_inversions']}; "
            f"cross-block inversions={result['cross_block_tag_time_inversions']}",
            flush=True,
        )

    report = {
        "package_version": __version__,
        "scope": "raw FILETIME fields, per-block header agreement, and physical-order Tag chronology",
        "sample_count": len(results),
        "total_numeric_records": sum(r["numeric_records"] for r in results),
        "all_timestamp_remainders_mod_10000_zero": all(
            r["all_timestamps_on_1ms_grid"] for r in results
        ),
        "all_header_first_record_timestamps_match": all(
            r["header_first_record_mismatches"] == 0 for r in results
        ),
        "no_within_block_or_cross_block_tag_time_inversions": all(
            r["within_block_time_inversions"] == 0
            and r["cross_block_tag_time_inversions"] == 0
            for r in results
        ),
        "results": results,
    }
    output = (
        args.output.expanduser().resolve()
        if args.output is not None
        else root / "timestamp_audit_report.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {output}", flush=True)


if __name__ == "__main__":
    main()
