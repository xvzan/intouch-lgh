"""NumPy- and Arrow-independent public data models for the v1 API."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator
import struct

BLOCK_SIZE = 512
HEADER_SIZE = 56
ENTRY_SIZE = 16
UINT64_SIZE = 8


@dataclass(frozen=True, slots=True)
class BlockRef:
    """A numeric LGH block identified by its physical position."""
    block_number: int
    block_offset: int
    tag: str
    value_type: str
    count: int


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Summary of the validation level completed for an Archive."""
    level: str
    lgh_size: int
    idx_size: int
    numeric_records: int | None = None
    numeric_blocks: int | None = None
    indexed_blocks: int | None = None
    index_pages: int | None = None
    # Optional diagnostics populated by validate="full". These fields are
    # additive so existing callers constructing/reading ValidationReport stay
    # source-compatible.
    idx_parse_seconds: float | None = None
    idx_lgh_header_check_seconds: float | None = None
    idx_coverage_check_seconds: float | None = None
    final_identity_check_seconds: float | None = None
    idx_verify_seconds: float | None = None
    lgh_block_scan_seconds: float | None = None
    validation_wall_seconds: float | None = None
    validation_thread_cpu_seconds: float | None = None
    validation_process_cpu_seconds: float | None = None
    idx_pages_parsed: int | None = None
    idx_entries_checked: int | None = None
    lgh_header_reads: int | None = None
    # Legacy 40-byte IDX Tag-header comparison work counter.
    lgh_header_bytes_requested: int | None = None
    # Actual fixed LGH header bytes inspected (plus blank-block tails if needed).
    lgh_block_header_bytes_read: int | None = None
    # Logical LGH address range covered by validation, not physical disk I/O.
    lgh_logical_bytes_scanned: int | None = None
    lgh_blocks_scanned: int | None = None


@dataclass(frozen=True, slots=True)
class RawBlock:
    """Block-level provenance for a contiguous row range in a RawBatch."""
    block_number: int
    block_offset: int
    tag: str
    value_type: str
    row_start: int
    row_stop: int


@dataclass(frozen=True, slots=True)
class RawBatch:
    """Detached columnar batch of original LGH fields.

    ``filetime_ticks`` and ``value_bits`` are independent immutable byte
    buffers. Each contains little-endian uint64 values in row order. The
    ``value_bits`` buffer is the authoritative original IEEE-754 payload;
    interpreting it as float64 is a view, not a conversion.

    ``record_indices`` stores the original uint8 position within the source
    LGH block. ``blocks`` maps contiguous row ranges to source block metadata.
    The batch owns its buffers and remains valid after Archive.close().

    The two uint64 columns are physically separate so NumPy and Arrow can
    construct primitive arrays directly from these buffers without
    de-interleaving or copying them.
    """
    filetime_ticks: bytes
    value_bits: bytes
    record_indices: bytes
    blocks: tuple[RawBlock, ...]
    source_file: str

    def __post_init__(self) -> None:
        row_count = len(self.record_indices)
        expected = row_count * UINT64_SIZE
        if len(self.filetime_ticks) != expected:
            raise ValueError("filetime_ticks must contain exactly 8 bytes per record")
        if len(self.value_bits) != expected:
            raise ValueError("value_bits must contain exactly 8 bytes per record")
        if row_count == 0:
            if self.blocks:
                raise ValueError("empty RawBatch cannot contain block segments")
            return
        cursor = 0
        for block in self.blocks:
            if block.row_start != cursor:
                raise ValueError("RawBlock row ranges must be contiguous and start at zero")
            if block.row_stop <= block.row_start or block.row_stop > row_count:
                raise ValueError("RawBlock row range is invalid")
            if block.block_number < 1 or block.block_offset < 0:
                raise ValueError("RawBlock source location is invalid")
            cursor = block.row_stop
        if cursor != row_count:
            raise ValueError("RawBlock segments must cover every RawBatch row")

    def __len__(self) -> int:
        return len(self.record_indices)

    @property
    def nbytes(self) -> int:
        return len(self.filetime_ticks) + len(self.value_bits) + len(self.record_indices)

    @property
    def filetime_buffer(self) -> memoryview:
        """Read-only byte view over little-endian FILETIME uint64 values."""
        return memoryview(self.filetime_ticks)

    @property
    def value_bits_buffer(self) -> memoryview:
        """Read-only byte view over original little-endian IEEE-754 bits."""
        return memoryview(self.value_bits)

    @property
    def record_indices_buffer(self) -> memoryview:
        return memoryview(self.record_indices)

    def iter_raw(self) -> Iterator[tuple[RawBlock, int, int, int]]:
        """Yield (block, record_index, FILETIME ticks, raw IEEE-754 bits)."""
        for block in self.blocks:
            for row in range(block.row_start, block.row_stop):
                ticks = struct.unpack_from("<Q", self.filetime_ticks, row * UINT64_SIZE)[0]
                bits = struct.unpack_from("<Q", self.value_bits, row * UINT64_SIZE)[0]
                yield block, self.record_indices[row], ticks, bits

    def iter_locations(self) -> Iterator[tuple[int, int, int]]:
        """Yield (block_offset, record_index, record_offset) per row."""
        for block in self.blocks:
            for row in range(block.row_start, block.row_stop):
                index = self.record_indices[row]
                yield (
                    block.block_offset,
                    index,
                    block.block_offset + HEADER_SIZE + index * ENTRY_SIZE,
                )
