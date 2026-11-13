from intouch_lgh.reader import LGHReader
"""Golden comparisons against PKUbuntu/intouch-dumper's published data and output.

The reference program rounds values and prints microsecond timestamps using
floating-point timedelta arithmetic. Timestamp discrepancies <= 2 us are
expected and do not indicate divergent FILETIME decoding.
"""
from collections import Counter
from datetime import datetime
from pathlib import Path
import math
import pytest


FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.mark.parametrize('stem,expected_count,metadata_count', [
    ('24072200', 412, 8),
    ('24072300', 4523, 16),
])
def test_public_lgh(stem, expected_count, metadata_count):
    reader = LGHReader(FIXTURES / f'{stem}.lgh')
    records = list(reader.records())
    assert len(records) == expected_count
    assert len(reader.metadata_blocks) == metadata_count
    assert not reader.unknown_blocks
    assert len(list(reader.blocks())) > 0
    assert all(rec.record_offset + 16 <= (rec.block_offset + 512) for rec in records)
    assert (FIXTURES / f'{stem}.idx').exists()


def test_24072300_matches_upstream_printed_output():
    """Independently compare every row against the published 4,971-line transcript."""
    rows = []
    for line in (FIXTURES / 'example_output.txt').read_text().splitlines():
        if not line.strip() or '----- Above are ' in line:
            continue
        parts = line.split()
        assert len(parts) == 4, line
        rows.append((parts[0], parts[1], datetime.fromisoformat(parts[2] + 'T' + parts[3])))
    records = list(LGHReader(FIXTURES / '24072300.lgh').records())
    assert len(rows) == len(records) == 4523
    for (tag, displayed_value, dt), rec in zip(rows, records, strict=True):
        assert tag == rec.tag
        if rec.value_type == 'REAL':
            assert math.isclose(float(displayed_value), rec.value, abs_tol=0.0051)
        else:
            assert round(float(displayed_value)) == round(rec.value)
        # Original reference computes datetime using a float seconds delta.
        assert abs((rec.timestamp_utc.replace(tzinfo=None) - dt).total_seconds()) <= 0.000002


def test_idx_are_present_but_not_interpreted():
    """IDX bytes must not be silently described as parsed by this library."""
    assert (FIXTURES / '24072200.idx').stat().st_size == 4096
    assert (FIXTURES / '24072300.idx').stat().st_size == 19456
