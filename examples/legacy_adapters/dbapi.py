"""DB-API 2.0 integration for immutable LGH record streams.

No driver is loaded until requested.  Database connections are owned by the
caller in ``write_dbapi``; ``write_postgres`` owns its psycopg connection.
This is *not* a checkpointed archive importer (see archive.py for SQLite).
"""
from __future__ import annotations

from typing import Iterable, Any
from intouch_lgh.reader import Record
from .validation import validate_export_record

_COLUMNS = (
    "source_file", "record_offset", "tag", "value_type", "value",
    "filetime_ticks", "timestamp_utc", "block_offset", "record_index",
)

_CREATE_SQL = {
    "sqlite": """CREATE TABLE IF NOT EXISTS lgh_records (
        source_file TEXT NOT NULL, record_offset INTEGER NOT NULL,
        tag TEXT NOT NULL, value_type TEXT NOT NULL, value REAL,
        filetime_ticks INTEGER NOT NULL, timestamp_utc TEXT NOT NULL,
        block_offset INTEGER NOT NULL, record_index INTEGER NOT NULL,
        PRIMARY KEY (source_file, record_offset))""",
    "postgresql": """CREATE TABLE IF NOT EXISTS lgh_records (
        source_file TEXT NOT NULL, record_offset BIGINT NOT NULL,
        tag TEXT NOT NULL, value_type TEXT NOT NULL, value DOUBLE PRECISION,
        filetime_ticks BIGINT NOT NULL, timestamp_utc TEXT NOT NULL,
        block_offset BIGINT NOT NULL, record_index INTEGER NOT NULL,
        value_bits_hex TEXT,
        PRIMARY KEY (source_file, record_offset))""",
}


def _row(r: Record, *, dialect: str = "postgresql") -> tuple:
    validate_export_record(r, sink=dialect)
    row = (r.source_file, r.record_offset, r.tag, r.value_type, r.value,
           r.filetime_ticks, r.timestamp_iso, r.block_offset, r.record_index)
    return row + (f"{r.value_bits:016x}",) if dialect == "postgresql" else row


def write_dbapi(records: Iterable[Record], connection: Any, *,
                dialect: str = "sqlite", batch_size: int = 1000,
                on_conflict: str = "error") -> int:
    """Write a stream to an existing SQLite or PostgreSQL DB-API connection.

    One transaction for the whole call: commit after all batches and rollback
    on any error. By default, duplicate source_file+record_offset raises an
    integrity error instead of hiding changed data. Set on_conflict="ignore"
    explicitly for legacy skip-duplicates behavior. Return records processed
    (which can include skipped records only with on_conflict="ignore").

    This helper uses a fixed known table name and parameterized values.
    Avoid using ``skip_unknown=True`` for source reads: incomplete reads cannot
    be detected downstream by this writer.
    """
    if dialect not in _CREATE_SQL:
        raise ValueError("dialect must be 'sqlite' or 'postgresql'")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if on_conflict not in ("error", "ignore"):
        raise ValueError("on_conflict must be error or ignore")
    placeholder = "?" if dialect == "sqlite" else "%s"
    columns = _COLUMNS + (("value_bits_hex",) if dialect == "postgresql" else ())
    insert = ("INSERT INTO lgh_records (" + ",".join(columns) + ") VALUES ("
              + ",".join([placeholder] * len(columns)) + ") "
              + ("ON CONFLICT (source_file,record_offset) DO NOTHING"
               if on_conflict == "ignore" else ""))
    count = 0
    cursor = connection.cursor()
    try:
        cursor.execute(_CREATE_SQL[dialect])
        if dialect == "postgresql":
            cursor.execute("ALTER TABLE lgh_records ADD COLUMN IF NOT EXISTS value_bits_hex TEXT")
        batch: list[tuple] = []
        for record in records:
            batch.append(_row(record, dialect=dialect))
            count += 1
            if len(batch) >= batch_size:
                cursor.executemany(insert, batch)
                batch.clear()
        if batch:
            cursor.executemany(insert, batch)
        connection.commit()
        return count
    except BaseException:
        connection.rollback()
        raise
    finally:
        cursor.close()


def write_postgres(records: Iterable[Record], dsn: str, *, batch_size: int = 1000,
                   on_conflict: str = "error") -> int:
    """Write records to PostgreSQL (also TimescaleDB) using psycopg 3.

    This imports the optional driver lazily. It does not create a hypertable,
    establish checkpoint semantics, or migrate an existing schema.
    """
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL output requires 'pip install .[postgres]' (psycopg 3)") from exc
    with psycopg.connect(dsn) as conn:
        return write_dbapi(records, conn, dialect="postgresql", batch_size=batch_size, on_conflict=on_conflict)
