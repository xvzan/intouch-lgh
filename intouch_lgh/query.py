"""Exact FILETIME range filters for experimental LGH records.

Ranges use [start, end), preserving on-disk 100 ns precision. The supplied
ISO timestamps must have explicit timezone offsets; naive datetimes are refused.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import re
from typing import Iterable, Iterator

from .reader import FILETIME_UNIX_EPOCH, Record

_ISO = re.compile(r'^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)(?:\.(\d{1,7}))?(Z|[+-]\d\d:\d\d)$')
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_filetime(value: str | datetime | int) -> int:
    """Convert an aware datetime, ISO8601 timestamp, or raw tick integer.

    ISO fractions can contain up to seven digits so tick granularity is exact;
    Python datetime values support only microseconds. No float arithmetic.
    """
    if isinstance(value, int):
        if value < 0 or value > 2**64 - 1:
            raise ValueError('FILETIME ticks must be an unsigned 64-bit integer')
        return value
    extra_tick = 0
    if isinstance(value, str):
        match = _ISO.fullmatch(value)
        if not match:
            raise ValueError('Expected ISO datetime with explicit timezone and at most 7 fractional digits')
        main, fraction, zone = match.groups()
        micro = (fraction or '').ljust(7, '0')
        extra_tick = int(micro[6])
        microsecond = int(micro[:6])
        if zone == 'Z':
            tz = timezone.utc
        else:
            hours = int(zone[1:3]); minutes = int(zone[4:6])
            if hours > 23 or minutes > 59:
                raise ValueError('Invalid timezone offset')
            shift = timedelta(hours=hours, minutes=minutes)
            tz = timezone(shift if zone[0] == '+' else -shift)
        value = datetime.fromisoformat(main).replace(tzinfo=tz, microsecond=microsecond)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Time bound must include an explicit timezone')
    delta = value.astimezone(timezone.utc) - _EPOCH
    ticks = FILETIME_UNIX_EPOCH + (delta.days * 86400 + delta.seconds) * 10_000_000 + delta.microseconds * 10 + extra_tick
    if ticks < 0 or ticks > 2**64 - 1:
        raise ValueError('Timestamp outside FILETIME range')
    return ticks


def filter_records(records: Iterable[Record], *, start: str | datetime | int | None = None,
                   end: str | datetime | int | None = None) -> Iterator[Record]:
    """Return records in [start, end), retaining input physical order.

    This is a record-level filter, NOT yet accelerated using IDX time nodes.
    It deliberately does not remove duplicates or reorder timestamps.
    """
    lo = to_filetime(start) if start is not None else None
    hi = to_filetime(end) if end is not None else None
    if lo is not None and hi is not None and hi < lo:
        raise ValueError('end must be >= start')
    for record in records:
        if (lo is None or record.filetime_ticks >= lo) and (hi is None or record.filetime_ticks < hi):
            yield record
