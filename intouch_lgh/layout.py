"""Non-destructive, evidence-based layout diagnostics for observed LGH/IDX files.

These observations are NOT a format specification. Deviations are reported,
not rejected merely because they differ from the two public fixtures.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import struct

from .reader import BLOCK_SIZE, HEADER_SIZE, ENTRY_SIZE, LGHReader, OPCODES
from .index import IDXReader, inspect_idx_topology


def inspect_archive_layout(lgh_path: str | Path, idx_path: str | Path | None = None) -> dict:
    """Inspect known blocks, inspect *all* decoded IDX node kinds, report anomalies.

    No database/file output, no sampling, and no interpretation of unknown
    IDX internal node references as LGH offsets. No silent fallback on errors.
    """
    reader = LGHReader(lgh_path)
    counts: Counter[str] = Counter()
    block_counts: Counter[str] = Counter()
    mismatch_offsets: list[int] = []
    nonzero_tail_offsets: list[int] = []
    # Diagnostics only: per-Tag chronology in physical file order.
    # Neither sorted input nor distinct timestamps is guaranteed by the format.
    tag_ranges: dict[str, list[int]] = {}
    last_ticks: dict[str, int] = {}
    chronology_inversions: list[dict[str, int | str]] = []
    same_timestamp_adjacent: Counter[str] = Counter()
    zero_block_offsets: list[int] = []
    file_size = Path(lgh_path).stat().st_size
    # The reader intentionally skips 512-byte all-zero blocks. Report them
    # here so a caller can decide whether this is expected padding.
    with Path(lgh_path).open('rb') as stream:
        for offset in range(0, file_size, BLOCK_SIZE):
            raw_page = stream.read(BLOCK_SIZE)
            if raw_page == bytes(BLOCK_SIZE):
                zero_block_offsets.append(offset)
    for info, raw in reader._blocks():
        block_counts[info.value_type] += 1
        counts[info.value_type] += info.count
        first_tick = struct.unpack_from('<Q', raw, HEADER_SIZE)[0]
        if info.header_filetime != first_tick:
            mismatch_offsets.append(info.offset)
        for i in range(info.count):
            pos = HEADER_SIZE + i * ENTRY_SIZE
            ticks = struct.unpack_from('<Q', raw, pos)[0]
            interval = tag_ranges.setdefault(info.tag, [ticks, ticks, 0])
            interval[0] = min(interval[0], ticks)
            interval[1] = max(interval[1], ticks)
            interval[2] += 1
            previous = last_ticks.get(info.tag)
            if previous is not None:
                if ticks < previous:
                    chronology_inversions.append({
                        'tag': info.tag,
                        'record_offset': info.offset + pos,
                        'previous_ticks': previous,
                        'ticks': ticks,
                    })
                elif ticks == previous:
                    same_timestamp_adjacent[info.tag] += 1
            last_ticks[info.tag] = ticks
        tail_start = HEADER_SIZE + info.count * ENTRY_SIZE
        if any(raw[tail_start:]):
            nonzero_tail_offsets.append(info.offset)
    result = {
        'lgh': str(lgh_path),
        'numeric_blocks': sum(block_counts.values()),
        'numeric_records': sum(counts.values()),
        'block_types': dict(sorted(block_counts.items())),
        'record_types': dict(sorted(counts.items())),
        'metadata_blocks': len(reader.metadata_blocks),
        'unknown_blocks': list(reader.unknown_blocks),
        'header_first_timestamp_mismatch_offsets': mismatch_offsets,
        'nonzero_unused_payload_offsets': nonzero_tail_offsets,
        'zero_block_offsets': zero_block_offsets,
        'tag_time_ranges': {
            tag: {'min_filetime_ticks': rng[0], 'max_filetime_ticks': rng[1], 'records': rng[2]}
            for tag, rng in sorted(tag_ranges.items())
        },
        'chronology_inversions': chronology_inversions,
        'adjacent_equal_timestamps_by_tag': dict(sorted(same_timestamp_adjacent.items())),
    }
    if idx_path is not None:
        idx = IDXReader(idx_path)
        pages = list(idx.pages())
        result['idx'] = {
            'path': str(idx_path),
            'page_kinds': dict(sorted(Counter(p.kind for p in pages).items())),
            'internal_pages': [
                {'page_number': p.page_number, 'node_id': p.node_id,
                 'aux': p.aux, 'entries': len(p.entries),
                 'references': [(e.block_number, e.tag) for e in p.entries]}
                for p in pages if p.kind == 0
            ],
            'leaf_pages': sum(p.kind == 1 for p in pages),
            'coverage': idx.verify_lgh(lgh_path),
            'topology': inspect_idx_topology(idx_path),
        }
    return result
