from __future__ import annotations

import os
import math
import struct
from collections import Counter
from pathlib import Path

import pytest

from intouch_lgh import Archive
from intouch_lgh.index import IDXReader, inspect_idx_topology


CORPUS = Path(os.environ["INTOUCH_LGH_CORPUS_DIR"]) if os.environ.get("INTOUCH_LGH_CORPUS_DIR") else None
FIXTURES = Path(__file__).parent / "fixtures"
BUNDLED_STEMS = {"24072200", "24072300"}


def _samples() -> tuple[str, ...]:
    stems = set(BUNDLED_STEMS)
    if CORPUS is not None and CORPUS.is_dir():
        stems.update(p.stem for p in CORPUS.glob("*.idx") if p.with_suffix(".lgh").is_file())
    return tuple(sorted(stems))


SAMPLES = _samples()


def _locate_pair(stem: str) -> tuple[Path, Path]:
    # Never combine an LGH file from one corpus with an IDX file from another.
    if CORPUS is not None:
        lgh = CORPUS / f"{stem}.lgh"
        idx = CORPUS / f"{stem}.idx"
        if lgh.is_file() and idx.is_file():
            return lgh, idx
    if stem in BUNDLED_STEMS:
        lgh = FIXTURES / f"{stem}.lgh"
        idx = FIXTURES / f"{stem}.idx"
        if lgh.is_file() and idx.is_file():
            return lgh, idx
    if CORPUS is None:
        pytest.skip(f"Set INTOUCH_LGH_CORPUS_DIR to enable external corpus test: {stem}")
    pytest.skip(f"External LGH/IDX pair not present: {stem}")


def _locate_idx(stem: str) -> Path:
    if CORPUS is not None:
        candidate = CORPUS / f"{stem}.idx"
        if candidate.is_file():
            return candidate
    if stem in BUNDLED_STEMS:
        candidate = FIXTURES / f"{stem}.idx"
        if candidate.is_file():
            return candidate
    pytest.skip(f"External IDX file not present: {stem}")

@pytest.mark.parametrize("stem", SAMPLES)
def test_external_archive_pair_full_validation_and_types(stem):
    lgh, idx = _locate_pair(stem)

    topology = inspect_idx_topology(idx)
    assert topology["matches_observed_layout"], topology["issues"]
    assert topology["reachable_page_count"] == topology["page_count"]
    assert topology["unreachable_page_ids"] == []
    assert topology["leaf_link_issue_count"] == 0

    type_rows = Counter()
    rows = 0
    with Archive.open(lgh, idx, validate="full") as archive:
        for batch in archive.iter_batches(batch_size=65536, validate_types=False):
            rows += len(batch)
            for block in batch.blocks:
                type_rows[block.value_type] += block.row_stop - block.row_start
                if block.value_type in ("BOOL", "INT"):
                    for row in range(block.row_start, block.row_stop):
                        value = struct.unpack_from("<d", batch.value_bits, row * 8)[0]
                        assert math.isfinite(value) and value.is_integer()
                        if block.value_type == "BOOL":
                            assert value in (0.0, 1.0)

    assert rows > 0
    assert sum(type_rows.values()) == rows
    assert set(type_rows).issubset({"BOOL", "INT", "REAL"})


@pytest.mark.parametrize("stem", SAMPLES)
def test_external_idx_tree_lookup_matches_single_scan(stem):
    idx_path = _locate_idx(stem)
    reader = IDXReader(idx_path)
    scan = {}
    for entry in reader.entries():
        scan.setdefault(entry.tag, []).append(entry.block_number)

    for tag, expected in scan.items():
        assert reader.blocks_for_tag(tag, method="tree") == sorted(expected), (
            stem, tag
        )
