"""Optional, bounded-memory Parquet export for InTouch LGH archives.

The adapter consumes detached RawBatch objects through the Arrow adapter. It
never constructs one Python Record per observation and never routes data
through CSV, Pandas, or Polars. Output is written to a temporary sibling file
and committed atomically only after the Parquet footer is closed successfully.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal
import os
import tempfile

from .arrow_adapter import iter_arrow_batches, to_arrow_batch
from .model import RawBatch

DEFAULT_PARQUET_COLUMNS = (
    "tag",
    "value_type",
    "filetime_ticks",
    "value_bits",
    "value",
    "block_offset",
    "record_index",
    "record_offset",
)


@dataclass(frozen=True, slots=True)
class ParquetExportResult:
    """Summary of one completed Parquet export."""

    path: Path
    rows_written: int
    batches_written: int
    row_groups_written: int
    file_size_bytes: int


def _pyarrow_parquet():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError(
            "Parquet export requires PyArrow; install with `pip install intouch-lgh[parquet]`"
        ) from exc
    return pa, pq


def _normalize_columns(columns: Iterable[str] | str | None) -> tuple[str, ...]:
    if columns is None:
        return DEFAULT_PARQUET_COLUMNS
    if isinstance(columns, str):
        result = (columns,)
    else:
        result = tuple(columns)
    if not result:
        raise ValueError("columns must select at least one column")
    if len(set(result)) != len(result):
        raise ValueError("columns must not contain duplicates")
    allowed = set(DEFAULT_PARQUET_COLUMNS)
    unknown = set(result) - allowed
    if unknown:
        raise ValueError(f"unsupported Parquet columns: {sorted(unknown)}")
    # Exact provenance is a core promise of the default export. Callers may
    # project columns, but if they request a value interpretation they should
    # retain the authoritative bit representation alongside it.
    if "value" in result and "value_bits" not in result:
        raise ValueError("'value' is an interpretation; include 'value_bits' for lossless export")
    return result


def _commit_temp(temp_path: Path, destination: Path, *, overwrite: bool) -> None:
    """Atomically publish a completed same-directory temporary file."""
    if overwrite:
        os.replace(temp_path, destination)
        return
    # A hard-link creation is atomic and fails if the destination already
    # exists. Both paths are siblings, so this stays on the same filesystem.
    # This avoids a check-then-replace race that could clobber another file.
    try:
        os.link(temp_path, destination)
    except FileExistsError:
        raise
    except OSError as exc:
        raise OSError(
            f"Cannot atomically publish {destination} without overwrite on this filesystem; "
            "use overwrite=True or choose a filesystem supporting hard links"
        ) from exc
    else:
        # The destination is already a complete hard link. Failure to remove
        # the temporary name must not turn a successful publication into a
        # reported export failure; the outer cleanup retries best-effort.
        try:
            temp_path.unlink()
        except OSError:
            pass


def export_parquet(
    archive,
    destination: str | os.PathLike[str],
    *,
    tags=None,
    start=None,
    end=None,
    batch_size: int = 65_536,
    row_group_size: int | None = None,
    columns: Iterable[str] | str | None = None,
    compression: str | None = "zstd",
    overwrite: bool = False,
    validate_types: bool = False,
    source_metadata: Literal["basename", "full", "none"] = "basename",
) -> ParquetExportResult:
    """Stream an open :class:`Archive` to one Parquet file.

    Parameters
    ----------
    archive:
        An already-open ``intouch_lgh.Archive``. The caller controls archive
        validation and lifetime.
    destination:
        Output Parquet path. Its parent directory must already exist.
    tags, start, end:
        Same Tag and half-open FILETIME/time filters as ``Archive.iter_batches``.
    batch_size:
        Maximum number of source records held in one RawBatch/Arrow batch.
    row_group_size:
        Maximum rows per Parquet row group. Defaults to ``batch_size``.
        Arrow batches are accumulated as chunks until complete row groups can
        be written. Primitive column buffers are not concatenated into new
        contiguous arrays. Pending data is bounded to approximately one row
        group plus one source batch.
    columns:
        Arrow/Parquet columns. By default all raw values and provenance fields
        are included. ``value_bits`` is authoritative for exact IEEE-754 bits.
    compression:
        PyArrow Parquet compression codec, e.g. ``zstd``, ``snappy``, ``gzip``
        or ``None``.
    overwrite:
        If false (default), fail if destination already exists. If true, replace
        it atomically after the complete output has been finalized.
    validate_types:
        Forwarded to ``Archive.iter_batches``.
    source_metadata:
        Controls the source-file schema metadata: ``basename`` (default,
        privacy-safe), ``full`` (absolute/source path), or ``none``.

    Returns
    -------
    ParquetExportResult
        Counts and final output size. A result is returned only after the
        temporary file has been finalized and atomically published.
    """
    pa, pq = _pyarrow_parquet()
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if row_group_size is not None and row_group_size <= 0:
        raise ValueError("row_group_size must be positive")

    selected = _normalize_columns(columns)
    if source_metadata not in {"basename", "full", "none"}:
        raise ValueError("source_metadata must be 'basename', 'full', or 'none'")
    destination = Path(destination).expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    # Resolve the containing directory, but not the final path component: if
    # the destination is a symlink, publication should replace that directory
    # entry rather than silently following it to another file.
    parent = destination.parent.resolve()
    destination = parent / destination.name
    if not parent.is_dir():
        raise FileNotFoundError(f"destination directory does not exist: {parent}")
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)

    effective_row_group_size = row_group_size or batch_size
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    published = False
    rows_written = 0
    batches_written = 0
    row_groups_written = 0

    batches = None
    try:
        batches = iter_arrow_batches(
            archive,
            tags=tags,
            start=start,
            end=end,
            batch_size=batch_size,
            validate_types=validate_types,
            columns=selected,
            source_metadata=source_metadata,
        )
        first = next(batches, None)
        if first is None:
            # Empty selections still produce a valid Parquet file with a stable
            # schema and archive provenance metadata.
            empty = RawBatch(
                filetime_ticks=b"",
                value_bits=b"",
                record_indices=b"",
                blocks=(),
                source_file=str(archive.lgh_path),
            )
            schema = to_arrow_batch(
                empty,
                columns=selected,
                source_metadata=source_metadata,
            ).schema
        else:
            schema = first.schema

        writer = pq.ParquetWriter(
            str(temp_path),
            schema,
            compression=compression,
            use_dictionary=([name for name in ("tag", "value_type") if name in selected] or False),
            write_statistics=True,
            store_schema=True,
        )
        try:
            pending_batches = []
            pending_rows = 0
            current = first
            while current is not None:
                if not current.schema.equals(schema, check_metadata=True):
                    raise ValueError(
                        "Arrow schema changed between batches; refusing to create an inconsistent Parquet file"
                    )
                if current.num_rows:
                    pending_batches.append(current)
                    pending_rows += current.num_rows
                rows_written += current.num_rows
                batches_written += 1

                # Accumulate Arrow batches until at least one complete Row
                # Group is available. Table construction is chunked/zero-copy:
                # it keeps references to the existing Arrow buffers rather
                # than concatenating every column into a new contiguous array.
                if pending_rows >= effective_row_group_size:
                    table = pa.Table.from_batches(
                        pending_batches, schema=schema
                    ).unify_dictionaries()
                    full_rows = (
                        table.num_rows // effective_row_group_size
                    ) * effective_row_group_size
                    if full_rows:
                        writer.write_table(
                            table.slice(0, full_rows),
                            row_group_size=effective_row_group_size,
                        )
                    remainder = table.slice(full_rows)
                    pending_batches = remainder.to_batches() if remainder.num_rows else []
                    pending_rows = remainder.num_rows
                    # Drop references to completed groups before asking the
                    # archive iterator for another source batch.
                    del table, remainder

                current = next(batches, None)

            # The final partial Row Group is written only after source
            # iteration completes. Empty selections retain their valid schema.
            if pending_rows:
                table = pa.Table.from_batches(
                    pending_batches, schema=schema
                ).unify_dictionaries()
                writer.write_table(
                    table,
                    row_group_size=effective_row_group_size,
                )
                del table
        finally:
            writer.close()

        # Re-open only the footer to report actual row-group count and verify
        # the completed file is a readable Parquet artifact before publication.
        metadata = pq.ParquetFile(str(temp_path)).metadata
        if metadata.num_rows != rows_written:
            raise RuntimeError(
                f"Parquet row count mismatch: footer={metadata.num_rows}, written={rows_written}"
            )
        row_groups_written = metadata.num_row_groups
        file_size_bytes = temp_path.stat().st_size
        _commit_temp(temp_path, destination, overwrite=overwrite)
        published = True
        return ParquetExportResult(
            path=destination,
            rows_written=rows_written,
            batches_written=batches_written,
            row_groups_written=row_groups_written,
            file_size_bytes=file_size_bytes,
        )
    finally:
        if batches is not None:
            close_batches = getattr(batches, "close", None)
            if close_batches is not None:
                close_batches()
        if not published:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
