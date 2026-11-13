"""Optional Apache Arrow adapter over detached RawBatch column buffers.

The core remains independent of PyArrow. Arrow primitive arrays are built
with ``Array.from_buffers`` over RawBatch-owned immutable byte buffers. The
FILETIME and IEEE-754 columns therefore require no copy. The float64 ``value``
array and uint64 ``value_bits`` array intentionally share the same value buffer.

Per-row dictionary indices and optional provenance columns are materialized by
the adapter because RawBatch stores those attributes compactly at block level.
"""
from __future__ import annotations

from array import array
from typing import Iterable, Iterator, Literal
from pathlib import Path, PureWindowsPath
import sys

from .model import RawBatch

DEFAULT_COLUMNS = (
    "tag",
    "value_type",
    "filetime_ticks",
    "value_bits",
    "value",
    "record_index",
)
VALID_COLUMNS = frozenset({
    "tag", "value_type", "filetime_ticks", "value_bits", "value",
    "record_index", "block_offset", "record_offset",
})


def _arrow():
    try:
        import pyarrow as pa
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(
            "Arrow support requires `pip install intouch-lgh[arrow]`"
        ) from exc
    return pa


def _pa_primitive(pa, typ, data: bytes, length: int):
    if len(data) != length * typ.bit_width // 8:
        raise ValueError(f"buffer length does not match Arrow {typ} length")
    return pa.Array.from_buffers(
        typ, length, [None, pa.py_buffer(data)], null_count=0
    )


def _array_bytes(values: array) -> bytes:
    """Return little-endian bytes for a native stdlib numeric array."""
    if values.itemsize > 1 and sys.byteorder != "little":
        values = array(values.typecode, values)
        values.byteswap()
    return values.tobytes()


def _repeated_array(typecode: str, values: Iterable[tuple[int, int]]) -> array:
    """Build a compact per-row integer array from (value, repeat_count)."""
    out = array(typecode)
    for value, count in values:
        if count:
            out.extend(array(typecode, [value]) * count)
    return out


def _dictionary_indices(pa, raw: RawBatch, *, field: str):
    if field == "tag":
        dictionary = tuple(dict.fromkeys(block.tag for block in raw.blocks))
        ids = {tag: i for i, tag in enumerate(dictionary)}
        runs = ((ids[block.tag], block.row_stop - block.row_start) for block in raw.blocks)
        # The index type must be stable across batches. Selecting uint8 or
        # uint16 independently from each batch's dictionary cardinality can
        # make otherwise compatible RecordBatches impossible to combine into
        # one Arrow Table or ParquetWriter stream.
        index_type = pa.uint32()
        if array("I").itemsize != 4:
            raise RuntimeError("array('I') is not a 32-bit unsigned buffer on this platform")
        if len(dictionary) > 0x1_0000_0000:
            raise OverflowError("Tag dictionary exceeds uint32 index capacity")
        indices = _repeated_array("I", runs)
        data = _array_bytes(indices)
        index_array = _pa_primitive(pa, index_type, data, len(raw))
        dictionary_array = pa.array(dictionary, type=pa.string())
        return pa.DictionaryArray.from_arrays(index_array, dictionary_array)

    if field == "value_type":
        dictionary = ("BOOL", "INT", "REAL")
        ids = {name: i for i, name in enumerate(dictionary)}
        runs = ((ids[block.value_type], block.row_stop - block.row_start)
                for block in raw.blocks)
        indices = _repeated_array("B", runs)
        index_array = _pa_primitive(pa, pa.uint8(), indices.tobytes(), len(raw))
        dictionary_array = pa.array(dictionary, type=pa.string())
        return pa.DictionaryArray.from_arrays(index_array, dictionary_array)

    raise ValueError(field)


def _provenance_array(pa, raw: RawBatch, *, field: str):
    if field == "block_offset":
        runs = ((block.block_offset, block.row_stop - block.row_start)
                for block in raw.blocks)
        values = _repeated_array("Q", runs)
    elif field == "record_offset":
        values = array("Q")
        for block in raw.blocks:
            for row in range(block.row_start, block.row_stop):
                index = raw.record_indices[row]
                values.append(block.block_offset + 56 + index * 16)
    else:
        raise ValueError(field)
    return _pa_primitive(pa, pa.uint64(), _array_bytes(values), len(raw))


def _normalize_columns(columns: Iterable[str] | str | None) -> tuple[str, ...]:
    if columns is None:
        return DEFAULT_COLUMNS
    if isinstance(columns, str):
        selected = (columns,)
    else:
        selected = tuple(columns)
    if not selected:
        raise ValueError("columns must select at least one column")
    if len(set(selected)) != len(selected):
        raise ValueError("columns must not contain duplicates")
    unknown = set(selected) - VALID_COLUMNS
    if unknown:
        raise ValueError(f"unknown Arrow columns: {sorted(unknown)}")
    return selected



def _source_file_metadata(source_file: str, policy: str) -> bytes | None:
    """Return a privacy-aware source-file metadata value.

    ``basename`` is the default so exported files do not disclose the source
    machine's username or directory structure. ``full`` preserves the path;
    ``none`` omits the key entirely.
    """
    if policy not in {"basename", "full", "none"}:
        raise ValueError("source_metadata must be 'basename', 'full', or 'none'")
    if policy == "none":
        return None
    if policy == "full":
        return source_file.encode("utf-8", errors="surrogateescape")
    # Handle Windows paths even when inspecting an artifact on POSIX.
    name = (PureWindowsPath(source_file).name
            if "\\" in source_file else Path(source_file).name)
    return name.encode("utf-8", errors="surrogateescape")

def to_arrow_batch(
    raw: RawBatch,
    *,
    columns: Iterable[str] | str | None = None,
    source_metadata: Literal["basename", "full", "none"] = "basename",
):
    """Convert one RawBatch into an Arrow RecordBatch.

    ``filetime_ticks``, ``value_bits``, ``value``, and ``record_index`` are
    zero-copy views over RawBatch-owned buffers. ``value`` and ``value_bits``
    share exactly the same 8-byte-per-row value buffer. Dictionary columns and
    optional per-row provenance columns require small adapter-side allocations.

    The returned Arrow arrays own references to their source Python buffers and
    remain valid after the source Archive is closed or the RawBatch is released.
    NaN/Inf are valid floating values, never converted to Arrow nulls.
    """
    pa = _arrow()
    selected = _normalize_columns(columns)
    if source_metadata not in {"basename", "full", "none"}:
        raise ValueError("source_metadata must be 'basename', 'full', or 'none'")

    n = len(raw)
    arrays = []
    fields = []
    for name in selected:
        if name == "tag":
            arr = _dictionary_indices(pa, raw, field="tag")
        elif name == "value_type":
            arr = _dictionary_indices(pa, raw, field="value_type")
        elif name == "filetime_ticks":
            arr = _pa_primitive(pa, pa.uint64(), raw.filetime_ticks, n)
        elif name == "value_bits":
            arr = _pa_primitive(pa, pa.uint64(), raw.value_bits, n)
        elif name == "value":
            # Same bytes as value_bits; only the Arrow logical type differs.
            arr = _pa_primitive(pa, pa.float64(), raw.value_bits, n)
        elif name == "record_index":
            arr = _pa_primitive(pa, pa.uint8(), raw.record_indices, n)
        else:
            arr = _provenance_array(pa, raw, field=name)
        arrays.append(arr)
        fields.append(pa.field(name, arr.type, nullable=False))

    metadata = {
        b"intouch_lgh.record_order": b"LGH physical block order; original within-block order",
        b"intouch_lgh.filetime_ticks": b"unsigned 64-bit raw FILETIME ticks; no timezone conversion",
        b"intouch_lgh.value_bits": b"authoritative raw IEEE-754 binary64 payload bits",
    }
    source_value = _source_file_metadata(raw.source_file, source_metadata)
    if source_value is not None:
        metadata[b"intouch_lgh.source_file"] = source_value
    schema = pa.schema(fields, metadata=metadata)
    return pa.RecordBatch.from_arrays(arrays, schema=schema)


def iter_arrow_batches(
    archive,
    *,
    tags=None,
    start=None,
    end=None,
    batch_size: int = 65_536,
    validate_types: bool = False,
    columns: Iterable[str] | str | None = None,
    source_metadata: Literal["basename", "full", "none"] = "basename",
) -> Iterator:
    """Yield Arrow RecordBatches from an open Archive.

    ``source_metadata`` controls whether the source filename, full path, or no
    path is embedded in each batch's schema metadata. The privacy-safe default
    is ``basename``.
    """
    # Normalize once: callers may pass a one-shot generator of column names.
    if source_metadata not in {"basename", "full", "none"}:
        raise ValueError("source_metadata must be 'basename', 'full', or 'none'")
    selected_columns = None if columns is None else _normalize_columns(columns)
    for raw in archive.iter_batches(
        tags=tags,
        start=start,
        end=end,
        batch_size=batch_size,
        validate_types=validate_types,
    ):
        yield to_arrow_batch(
            raw,
            columns=selected_columns,
            source_metadata=source_metadata,
        )
