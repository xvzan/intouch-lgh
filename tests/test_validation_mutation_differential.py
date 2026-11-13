"""Deterministic malformed-file differential against the pre-optimization path.

All random mutations are confined to temporary copies of two real fixtures;
no consumer archives are changed.  Comparisons cover *accept/reject* parity,
not exact wording of error messages, which remains an independent compatibility
concern.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import random

import pytest

from intouch_lgh import Archive, FormatError
from intouch_lgh.index import IDXReader

FIXTURES = Path(__file__).parent / "fixtures"


def _legacy(lgh: Path, idx: Path):
    # Pre-optimization full audit: IDX cross-file verification, followed by
    # an independent sequential LGH scan and per-Tag count aggregation.
    original_index_report = IDXReader(idx).verify_lgh(lgh)
    index_report = {name: original_index_report[name] for name in ("indexed_blocks", "index_pages")}
    with Archive.open(lgh, idx, validate="none") as archive:
        counts = Counter()
        blocks = 0
        for block in archive._scan_blocks():
            counts[block.tag] += block.count
            blocks += 1
        return index_report, dict(counts), blocks


def _optimized(lgh: Path, idx: Path):
    with Archive.open(lgh, idx, validate="full") as archive:
        report = archive.validation_report
        return {
            "indexed_blocks": report.indexed_blocks,
            "index_pages": report.index_pages,
        }, dict(archive.tag_counts), report.numeric_blocks


@pytest.mark.parametrize("stem", ["24072200", "24072300"])
def test_mutated_archive_differential_200_cases(stem: str, tmp_path: Path):
    rng = random.Random(20261113)
    source_lgh = (FIXTURES / f"{stem}.lgh").read_bytes()
    source_idx = (FIXTURES / f"{stem}.idx").read_bytes()
    lgh_path, idx_path = tmp_path / "case.lgh", tmp_path / "case.idx"
    rejected = accepted = 0
    for case in range(200):
        lgh, idx = bytearray(source_lgh), bytearray(source_idx)
        targets = rng.choice(((lgh,), (lgh,), (idx,), (lgh, idx)))
        for data in targets:
            first_mutable_offset = 512 if data is lgh else 1024
            offset = rng.randrange(first_mutable_offset, len(data))
            action = rng.randrange(4)
            if action == 0:
                data[offset] ^= 1 << rng.randrange(8)
            elif action == 1:
                data[offset] = rng.randrange(256)
            elif action == 2:
                n = min(4, len(data) - offset)
                data[offset:offset+n] = b"\0" * n
            else:
                data[offset] = 255
        lgh_path.write_bytes(lgh)
        idx_path.write_bytes(idx)
        try:
            reference = _legacy(lgh_path, idx_path)
        except FormatError:
            reference = None
        try:
            candidate = _optimized(lgh_path, idx_path)
        except FormatError:
            candidate = None
        assert (reference is None) == (candidate is None), (stem, case)
        if reference is None:
            rejected += 1
        else:
            assert reference == candidate, (stem, case)
            accepted += 1
    assert rejected > 0 and accepted > 0

@pytest.mark.parametrize("stem", ["24072200", "24072300"])
def test_targeted_structure_mutations_200_cases(stem: str, tmp_path: Path):
    """Corrupt LGH fixed headers and IDX leaf fields, not merely value bytes."""
    import struct

    rng = random.Random(8021)
    source_lgh = (FIXTURES / f"{stem}.lgh").read_bytes()
    source_idx = (FIXTURES / f"{stem}.idx").read_bytes()
    idx_offsets: list[int] = []
    for page in range(1, len(source_idx) // 1024):
        start = page * 1024
        kind, count = struct.unpack_from("<HH", source_idx, start)
        if kind != 1:
            continue
        idx_offsets.extend(range(start, start + 12 + 4 * count))
        idx_offsets.extend(range(start + 12 + 4 * count,
                                 start + min(1024, 12 + 4 * count + 100)))
    assert idx_offsets
    lgh_path, idx_path = tmp_path / "case.lgh", tmp_path / "case.idx"
    rejected = 0
    for case in range(200):
        lgh, idx = bytearray(source_lgh), bytearray(source_idx)
        if rng.randrange(2) == 0:
            block = rng.randrange(2, len(lgh) // 512 + 1)
            offset = (block - 1) * 512 + rng.choice((*range(8), *range(16, 56)))
            lgh[offset] ^= 1 << rng.randrange(8)
        else:
            offset = rng.choice(idx_offsets)
            idx[offset] ^= 1 << rng.randrange(8)
        lgh_path.write_bytes(lgh)
        idx_path.write_bytes(idx)
        try:
            reference = _legacy(lgh_path, idx_path)
        except FormatError:
            reference = None
        try:
            candidate = _optimized(lgh_path, idx_path)
        except FormatError:
            candidate = None
        assert (reference is None) == (candidate is None), (stem, case)
        if reference is None:
            rejected += 1
        else:
            assert reference == candidate, (stem, case)
    assert rejected > 0
