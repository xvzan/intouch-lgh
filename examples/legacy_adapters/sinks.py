"""Export sinks for *observed* LGH records; no resampling or deduplication.

File outputs are staged beside their destination and atomically replaced only
when the complete record iterator and writer succeed. This prevents a corrupt
source encountered late in a scan from overwriting a previous good export.
"""
from __future__ import annotations

import json
import math
import struct
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Iterable

from intouch_lgh.reader import Record
from .validation import validate_export_record


def record_dict(r: Record, *, sink: str = "jsonl", nan_policy: str = "error") -> dict:
    validate_export_record(r, sink=sink, nan_policy=nan_policy)
    value = r.value
    if sink == "jsonl" and not math.isfinite(value):
        value = None  # Explicitly accompanied by exact bits below.
    return dict(tag=r.tag, value_type=r.value_type, value=value,
                value_bits_hex=f"{r.value_bits:016x}",
                filetime_ticks=r.filetime_ticks, timestamp_utc=r.timestamp_iso,
                source_file=r.source_file, block_offset=r.block_offset,
                record_offset=r.record_offset, record_index=r.record_index)


def _stage(destination: str | Path) -> tuple[Path, Path]:
    target = Path(destination)
    fd, name = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.partial', dir=target.parent)
    os.close(fd)
    return target, Path(name)


def write_jsonl(records: Iterable[Record], destination: str | Path, *, nan_policy: str = "error") -> int:
    if nan_policy not in ("error", "bits"):
        raise ValueError("nan_policy must be error or bits")
    target, staged = _stage(destination)
    count = 0
    try:
        with staged.open('w', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(record_dict(r, sink="jsonl", nan_policy=nan_policy), ensure_ascii=False, allow_nan=False) + '\n')
                count += 1
        os.replace(staged, target)
        return count
    finally:
        staged.unlink(missing_ok=True)


def write_sqlite(records: Iterable[Record], destination: str | Path, *,
                 batch_size: int = 1000, on_conflict: str = "error", nan_policy: str = "error") -> int:
    """Insert records transactionally; return rows processed, not newly inserted.

    SQLite is a persistent database: unlike file exports, the existing database
    is retained and insertions are rolled back on failure. Identical
    (source_file, record_offset) keys are ignored, not overwritten.
    """
    if batch_size <= 0:
        raise ValueError('batch_size must be positive')
    if on_conflict not in ("error", "ignore"):
        raise ValueError("on_conflict must be error or ignore")
    if nan_policy not in ("error", "bits"):
        raise ValueError("nan_policy must be error or bits")
    conn = sqlite3.connect(str(destination))
    count = 0
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS lgh_records (
            source_file TEXT NOT NULL, record_offset INTEGER NOT NULL,
            tag TEXT NOT NULL, value_type TEXT NOT NULL, value REAL,
            filetime_ticks INTEGER NOT NULL, timestamp_utc TEXT NOT NULL,
            block_offset INTEGER NOT NULL, record_index INTEGER NOT NULL,
            value_bits BLOB,
            PRIMARY KEY(source_file, record_offset))''')
        # Existing v0.11 databases do not have value_bits yet.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(lgh_records)")}
        if "value_bits" not in cols:
            conn.execute("ALTER TABLE lgh_records ADD COLUMN value_bits BLOB")
        batch = []
        def flush():
            if batch:
                verb = 'INSERT OR IGNORE' if on_conflict == 'ignore' else 'INSERT'
                conn.executemany(f'''{verb} INTO lgh_records
                    (source_file,record_offset,tag,value_type,value,filetime_ticks,timestamp_utc,block_offset,record_index,value_bits)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''', batch)
                batch.clear()
        for r in records:
            validate_export_record(r, sink="sqlite", nan_policy=nan_policy)
            # sqlite3 binds NaN as NULL; explicit bits mode preserves original payload.
            storage_value = None if math.isnan(r.value) else r.value
            batch.append((r.source_file, r.record_offset, r.tag, r.value_type, storage_value,
                          r.filetime_ticks, r.timestamp_iso, r.block_offset, r.record_index,
                          sqlite3.Binary(struct.pack("<Q", r.value_bits))))
            count += 1
            if len(batch) >= batch_size:
                flush()
        flush()
        conn.commit()
        return count
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def write_parquet(records: Iterable[Record], destination: str | Path, *, batch_size: int = 10000) -> int:
    """Atomically export Parquet, retaining original FILETIME ticks as int64.

    timestamp_utc is a convenience string with full 100-ns decimal precision; the
    original 100-ns clock reading is stored in filetime_ticks. No conversion to
    Arrow timestamp units occurs, so 100-ns data are not rounded away.
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError("Parquet output requires 'pip install .[parquet]'") from e
    if batch_size <= 0:
        raise ValueError('batch_size must be positive')
    schema = pa.schema([
        ('tag', pa.string()), ('value_type', pa.string()), ('value', pa.float64()),
        ('value_bits_hex', pa.string()),
        ('filetime_ticks', pa.int64()), ('timestamp_utc', pa.string()),
        ('source_file', pa.string()), ('block_offset', pa.int64()),
        ('record_offset', pa.int64()), ('record_index', pa.int64())])
    target, staged = _stage(destination)
    count = 0
    batch = []
    try:
        with pq.ParquetWriter(str(staged), schema) as writer:
            for record in records:
                batch.append(record_dict(record, sink="parquet"))
                if len(batch) >= batch_size:
                    writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                    count += len(batch)
                    batch.clear()
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                count += len(batch)
        os.replace(staged, target)
        return count
    finally:
        staged.unlink(missing_ok=True)
