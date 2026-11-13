"""Read a *hypothesized* 512-byte legacy-style LGH record block layout.

Based on observable behavior in the MIT-licensed PKUbuntu/intouch-dumper.
Not a general or official LGH format specification. This module does not
interpret IDX and does not support compressed, multi-block or newer layouts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import struct
import math
from typing import Iterator

BLOCK_SIZE = 512
_ZERO_BLOCK = bytes(BLOCK_SIZE)
HEADER_SIZE = 56
ENTRY_SIZE = 16
MAX_ENTRIES = (BLOCK_SIZE - HEADER_SIZE) // ENTRY_SIZE
OPCODES = {b"\xec\x13\x01\x00": "BOOL", b"\xec\x13\x02\x00": "INT", b"\xec\x13\x03\x00": "REAL"}
# Metadata observed in public fixtures and additional 2025 archive samples. Not a format specification.
SYSTEM_OPCODES = {b"\xec\x13\x14\x00": "$HIST_CREATE",
                  b"\xec\x13\x15\x00": "$HIST_CONNECT",
                  b"\xec\x13\x16\x00": "$HIST_ROLLIN",
                  b"\xec\x13\x17\x00": "$HIST_ROLLOVER",
                  b"\xec\x13\x18\x00": "$HIST_CLOSE"}
MARKER_OPCODE = b"\x00\x00\x0a\x00"
FILETIME_UNIX_EPOCH = 116444736000000000
# Largest FILETIME value representable by Python's datetime (9999-12-31
# 23:59:59.999999 UTC). Validate as an integer during iteration; constructing
# a datetime for every record is needlessly expensive when callers only need
# raw ticks and values.
FILETIME_MAX_TICKS = 2650467743999999999
_ENTRY_VALUE = struct.Struct("<Qd")
_VALUE_BITS = struct.Struct("<Q")


class FormatError(ValueError):
    """Input does not meet the supported experimental layout."""


@dataclass(frozen=True, slots=True)
class BlockInfo:
    offset: int
    tag: str
    value_type: str
    count: int
    header_filetime: int


@dataclass(frozen=True, slots=True)
class Record:
    tag: str
    value_type: str
    value: float  # On-disk payload is read as f64 even for BOOL/INT: see README.
    filetime_ticks: int  # Exact 100ns units, never rounded.
    source_file: str
    block_offset: int
    record_offset: int
    record_index: int
    raw_value_bits: int | None = None  # Exact on-disk bits (including NaN payload).

    @property
    def value_bits(self) -> int:
        """IEEE-754 payload as an unsigned 64-bit integer; raw if available."""
        if self.raw_value_bits is not None:
            return self.raw_value_bits
        return struct.unpack("<Q", struct.pack("<d", self.value))[0]

    @property
    def logical_value(self) -> bool | int | float:
        """Decode the community-observed semantic type without changing raw f64.

        For BOOL/INT, reject non-integral or non-finite payloads rather than
        silently truncating them. REAL returns the original Python float.
        This is an *interpretation* of the known opcodes, not a claim about all
        InTouch LGH format revisions.
        """
        if self.value_type == "REAL":
            return self.value
        if not math.isfinite(self.value) or not self.value.is_integer():
            raise FormatError(f"Invalid {self.value_type} payload {self.value!r} at 0x{self.record_offset:x}")
        if self.value_type == "BOOL":
            if self.value not in (0.0, 1.0):
                raise FormatError(f"Invalid BOOL payload {self.value!r} at 0x{self.record_offset:x}")
            return bool(self.value)
        if self.value_type == "INT":
            return int(self.value)
        raise FormatError(f"Unsupported logical value type: {self.value_type}")

    @property
    def timestamp_utc(self) -> datetime:
        # Python datetime has microsecond precision; filetime_ticks retains 100ns.
        _validate_filetime_ticks(self.filetime_ticks)
        microseconds, _sub_us_tick = divmod(self.filetime_ticks - FILETIME_UNIX_EPOCH, 10)
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=microseconds)

    @property
    def timestamp_iso(self) -> str:
        """UTC ISO8601 preserving the on-disk 100-ns fraction exactly.

        Python datetime rounds to microseconds, so derive the seventh decimal
        digit from the original FILETIME integer rather than a float.
        """
        dt = self.timestamp_utc
        seventh = self.filetime_ticks % 10
        if seventh:
            # Build the date/time prefix with datetime.isoformat rather than
            # strftime("%Y"), whose year zero-padding is platform-dependent.
            prefix = dt.isoformat(timespec="seconds")
            if prefix.endswith("+00:00"):
                prefix = prefix[:-6]
            return prefix + f".{dt.microsecond:06d}{seventh}+00:00"
        return dt.isoformat()



def _validate_filetime_ticks(ticks: int) -> None:
    """Validate FILETIME using integer bounds without allocating datetime objects."""
    if not 0 <= ticks <= FILETIME_MAX_TICKS:
        raise FormatError(f"FILETIME outside Python datetime range: {ticks}")


class LGHReader:
    def __init__(self, filename: str | Path, *, skip_unknown: bool = False, encoding: str = "utf-8", validate_types: bool = False):
        self.filename = Path(filename)
        self.skip_unknown = skip_unknown
        self.encoding = encoding
        self.validate_types = validate_types
        self.unknown_blocks: list[int] = []
        self.metadata_blocks: list[tuple[int, str]] = []

    def _blocks(self) -> Iterator[tuple[BlockInfo, bytes]]:
        with self.filename.open("rb") as stream:
            offset = 0
            while chunk := stream.read(BLOCK_SIZE):
                if len(chunk) != BLOCK_SIZE:
                    raise FormatError(f"Truncated block at 0x{offset:x}: {len(chunk)} bytes")
                if chunk == _ZERO_BLOCK:
                    offset += BLOCK_SIZE
                    continue
                opcode = chunk[:4]
                # The public LGH fixtures begin with a 512-byte file control
                # block and include special system records and a dated marker.
                if offset == 0 and chunk[:4] == b"\0" * 4 and chunk[8:12] == bytes.fromhex("000400fc"):
                    self.metadata_blocks.append((offset, "file_header"))
                    offset += BLOCK_SIZE
                    continue
                if opcode in SYSTEM_OPCODES:
                    actual = chunk[16:56].split(b"\0", 1)[0]
                    if actual != SYSTEM_OPCODES[opcode].encode("ascii") or int.from_bytes(chunk[4:8], "little") != 1:
                        raise FormatError(f"Unrecognized system block structure at 0x{offset:x}")
                    self.metadata_blocks.append((offset, SYSTEM_OPCODES[opcode]))
                    offset += BLOCK_SIZE
                    continue
                if opcode == MARKER_OPCODE and chunk[16:17] == b"$" and chunk[4:8] == b"\0" * 4:
                    self.metadata_blocks.append((offset, "date_marker"))
                    offset += BLOCK_SIZE
                    continue
                if opcode not in OPCODES:
                    self.unknown_blocks.append(offset)
                    if not self.skip_unknown:
                        raise FormatError(f"Unknown block type {opcode.hex()} at 0x{offset:x}; use skip_unknown only for exploration")
                    offset += BLOCK_SIZE
                    continue
                count = int.from_bytes(chunk[4:8], "little")
                if not (1 <= count <= MAX_ENTRIES):
                    raise FormatError(f"Invalid record count {count} at 0x{offset:x} (max {MAX_ENTRIES})")
                raw_tag = chunk[16:56].split(b"\0", 1)[0]
                if not raw_tag:
                    raise FormatError(f"Missing tag at 0x{offset:x}")
                try:
                    tag = raw_tag.decode(self.encoding)
                except UnicodeDecodeError as e:
                    raise FormatError(f"Cannot decode tag at 0x{offset:x} using {self.encoding}") from e
                header_time = int.from_bytes(chunk[8:16], "little")
                yield BlockInfo(offset, tag, OPCODES[opcode], count, header_time), chunk
                offset += BLOCK_SIZE

    def blocks(self) -> Iterator[BlockInfo]:
        for info, _ in self._blocks():
            yield info

    def records(self, *, tags: set[str] | None = None) -> Iterator[Record]:
        """Yield actual encoded observations without deduplication/resampling."""
        self.unknown_blocks.clear()
        self.metadata_blocks.clear()
        source_file = str(self.filename)
        for info, chunk in self._blocks():
            if tags is not None and info.tag not in tags:
                continue
            for index in range(info.count):
                start = HEADER_SIZE + index * ENTRY_SIZE
                ticks, value = _ENTRY_VALUE.unpack_from(chunk, start)
                bits = _VALUE_BITS.unpack_from(chunk, start + 8)[0]
                # Integer range validation is equivalent to timestamp_utc's
                # representability check, without allocating a datetime per row.
                if ticks > FILETIME_MAX_TICKS:  # unpacked as unsigned uint64
                    raise FormatError(f"FILETIME outside Python datetime range: {ticks}")
                record = Record(info.tag, info.value_type, value, ticks, source_file, info.offset, info.offset + start, index, bits)
                if self.validate_types:
                    _ = record.logical_value
                yield record

    def iter_batches(self, batch_size: int = 10_000, *, tags: set[str] | None = None) -> Iterator[list[Record]]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        batch: list[Record] = []
        for record in self.records(tags=tags):
            batch.append(record)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
