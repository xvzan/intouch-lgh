from __future__ import annotations

from intouch_lgh_diagnostics.archive_boundaries import (
    compare_tag_boundaries,
    summarize_forward_deltas,
)


def _tag(first, last, value_type="REAL"):
    return {
        "value_type": value_type,
        "first_filetime_ticks": first,
        "last_filetime_ticks": last,
        "first_timestamp_utc": "first",
        "last_timestamp_utc": "last",
        "first_block_number": 1,
        "last_block_number": 2,
        "first_record_index": 0,
        "last_record_index": 4,
    }


def test_forward_boundary_reports_exact_delta_without_gap_claim():
    result = compare_tag_boundaries(
        {"A": _tag(100, 200)},
        {"A": _tag(250, 300)},
    )[0]
    assert result["status"] == "forward"
    assert result["delta_ticks"] == 50
    assert result["delta_seconds"] == 50 / 10_000_000


def test_equal_timestamp_boundary_is_not_deduplicated_or_called_duplicate_data():
    result = compare_tag_boundaries(
        {"A": _tag(100, 200)},
        {"A": _tag(200, 300)},
    )[0]
    assert result["status"] == "equal_timestamp_boundary"
    assert result["delta_ticks"] == 0


def test_backward_boundary_is_reported_as_overlap():
    result = compare_tag_boundaries(
        {"A": _tag(100, 300)},
        {"A": _tag(250, 400)},
    )[0]
    assert result["status"] == "backward_overlap"
    assert result["delta_ticks"] == -50


def test_missing_and_new_tags_are_distinguished():
    results = {
        item["tag"]: item
        for item in compare_tag_boundaries(
            {"OLD": _tag(1, 2)},
            {"NEW": _tag(3, 4)},
        )
    }
    assert results["OLD"]["status"] == "missing_from_current"
    assert results["NEW"]["status"] == "appears_in_current_only"


def test_value_type_change_is_reported_independently():
    result = compare_tag_boundaries(
        {"A": _tag(100, 200, "INT")},
        {"A": _tag(250, 300, "REAL")},
    )[0]
    assert result["status"] == "forward"
    assert result["value_type_changed"] is True


def test_forward_delta_summary_is_descriptive_and_uses_nearest_rank():
    comparisons = [
        {"status": "forward", "delta_seconds": 1.0},
        {"status": "forward", "delta_seconds": 2.0},
        {"status": "forward", "delta_seconds": 3.0},
        {"status": "backward_overlap", "delta_seconds": -1.0},
    ]
    result = summarize_forward_deltas(comparisons)
    assert result == {
        "count": 3,
        "min_seconds": 1.0,
        "median_seconds": 2.0,
        "p90_seconds": 3.0,
        "max_seconds": 3.0,
    }


def test_forward_delta_summary_is_none_when_no_forward_boundaries():
    assert summarize_forward_deltas([
        {"status": "equal_timestamp_boundary", "delta_seconds": 0.0}
    ]) is None
