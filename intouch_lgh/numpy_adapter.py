"""Optional zero-copy NumPy views over core RawBatch buffers.

Importing the core package does not import NumPy. This adapter requires the
optional ``numpy`` extra and never changes the underlying raw IEEE-754 bits.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from .model import RawBatch


@dataclass(frozen=True, slots=True)
class NumpyBatch:
    """NumPy views and compact block metadata for one core RawBatch.

    With ``copy=False`` (the default), the record columns are contiguous,
    read-only views directly over the independent immutable byte buffers owned
    by RawBatch. ``values`` is a float64 view of ``value_bits``. With
    ``copy=True``, the two 64-bit columns are copied into writable contiguous
    arrays; ``values`` remains a view of ``value_bits``.
    """
    raw: RawBatch
    filetime_ticks: Any
    value_bits: Any
    record_indices: Any
    block_offsets: Any
    block_starts: Any
    block_tag_ids: Any
    type_codes: Any
    tag_dictionary: tuple[str, ...]
    copied: bool = False

    def __len__(self) -> int:
        return len(self.raw)

    @property
    def values(self):
        return self.value_bits.view("<f8")

    @property
    def source_file(self) -> str:
        return self.raw.source_file

    @property
    def blocks(self):
        return self.raw.blocks

    @property
    def record_offsets(self):
        import numpy as np
        counts = np.diff(self.block_starts).astype(np.intp, copy=False)
        block_rows = np.repeat(np.arange(len(self.block_offsets), dtype=np.intp), counts)
        return (
            self.block_offsets[block_rows]
            + 56
            + self.record_indices.astype("<u8", copy=False) * 16
        )

    @property
    def row_tags(self) -> tuple[str, ...]:
        ids = self.block_tag_ids
        counts = self.block_starts[1:] - self.block_starts[:-1]
        return tuple(
            self.tag_dictionary[int(tag_id)]
            for tag_id, count in zip(ids, counts, strict=True)
            for _ in range(int(count))
        )

    @property
    def row_value_types(self) -> tuple[str, ...]:
        import numpy as np
        code_to_name = {1: "BOOL", 2: "INT", 3: "REAL"}
        codes = np.repeat(
            self.type_codes,
            np.diff(self.block_starts).astype(np.intp, copy=False),
        )
        return tuple(code_to_name[int(code)] for code in codes)


def to_numpy_batch(raw: RawBatch, *, copy: bool = False) -> NumpyBatch:
    """Create NumPy arrays from a RawBatch.

    ``copy=False`` performs no copy between the RawBatch byte buffer and NumPy.
    The RawBatch itself owns a detached copy of selected LGH entry bytes, so
    NumPy arrays never pin Archive's mmap and remain valid after it is closed.
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "NumPy support requires `pip install intouch-lgh[numpy]`"
        ) from exc

    expected = len(raw) * 8
    if len(raw.filetime_ticks) != expected or len(raw.value_bits) != expected:
        raise ValueError("RawBatch uint64 column length does not match record count")

    ticks = np.frombuffer(raw.filetime_ticks, dtype="<u8", count=len(raw))
    bits = np.frombuffer(raw.value_bits, dtype="<u8", count=len(raw))
    if copy:
        ticks = np.array(ticks, dtype="<u8", copy=True)
        bits = np.array(bits, dtype="<u8", copy=True)

    record_indices = np.frombuffer(raw.record_indices, dtype="u1", count=len(raw))
    tag_dictionary = tuple(dict.fromkeys(block.tag for block in raw.blocks))
    tag_ids = {tag: i for i, tag in enumerate(tag_dictionary)}
    block_tag_ids = np.asarray([tag_ids[b.tag] for b in raw.blocks], dtype="<u4")
    type_codes_map = {"BOOL": 1, "INT": 2, "REAL": 3}
    type_codes = np.asarray(
        [type_codes_map[b.value_type] for b in raw.blocks], dtype="u1"
    )
    block_offsets = np.asarray([b.block_offset for b in raw.blocks], dtype="<u8")
    block_starts = np.asarray(
        [raw.blocks[0].row_start, *(b.row_stop for b in raw.blocks)]
        if raw.blocks else [0],
        dtype="<u8",
    )

    # RawBatch-backed views are immutable. Explicit copies of the two numeric
    # columns are independent and writable; compact metadata remains read-only.
    if not copy:
        ticks.flags.writeable = False
        bits.flags.writeable = False
    for array in (record_indices, block_tag_ids, type_codes, block_offsets, block_starts):
        array.flags.writeable = False

    return NumpyBatch(
        raw=raw,
        filetime_ticks=ticks,
        value_bits=bits,
        record_indices=record_indices,
        block_offsets=block_offsets,
        block_starts=block_starts,
        block_tag_ids=block_tag_ids,
        type_codes=type_codes,
        tag_dictionary=tag_dictionary,
        copied=copy,
    )


def iter_numpy_batches(archive, *, tags=None, start=None, end=None,
                       batch_size: int = 65_536, copy: bool = False) -> Iterator[NumpyBatch]:
    """Adapt ``Archive.iter_batches`` to optional NumPy views."""
    for raw in archive.iter_batches(
        tags=tags, start=start, end=end, batch_size=batch_size
    ):
        yield to_numpy_batch(raw, copy=copy)
