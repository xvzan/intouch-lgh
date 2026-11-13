"""Checkpointed PostgreSQL imports of *immutable* LGH archives.

Each file is imported in one DB transaction containing its records and
checkpoint. This module deliberately does not tail active LGH files.

A connection passed to ``sync_postgres_archive_connection`` must be in normal
transactional mode (autocommit disabled), with no pending caller transaction.
The importer owns commit/rollback for each archive. It is not concurrency-safe
with other importers targeting the same source names.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .archive import _digest
from intouch_lgh.query import filter_records, to_filetime
from intouch_lgh.reader import FormatError, LGHReader
from .validation import validate_export_record

CREATE_RECORDS = """CREATE TABLE IF NOT EXISTS lgh_records (
    source_file TEXT NOT NULL, record_offset BIGINT NOT NULL,
    tag TEXT NOT NULL, value_type TEXT NOT NULL, value DOUBLE PRECISION,
    filetime_ticks BIGINT NOT NULL, timestamp_utc TEXT NOT NULL,
    block_offset BIGINT NOT NULL, record_index INTEGER NOT NULL,
    value_bits_hex TEXT,
    PRIMARY KEY(source_file, record_offset))"""
CREATE_IMPORTS = """CREATE TABLE IF NOT EXISTS lgh_imports (
    source_file TEXT PRIMARY KEY, file_sha256 TEXT NOT NULL,
    query_key TEXT NOT NULL, record_count BIGINT NOT NULL)"""
INSERT_RECORDS = """INSERT INTO lgh_records
    (source_file,record_offset,tag,value_type,value,filetime_ticks,timestamp_utc,block_offset,record_index,value_bits_hex)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (source_file,record_offset) DO NOTHING"""
INSERT_CHECKPOINT = """INSERT INTO lgh_imports
    (source_file,file_sha256,query_key,record_count) VALUES (%s,%s,%s,%s)"""


def _query_key(*, start: Any, end: Any, tags: set[str] | None) -> tuple[int | None, int | None, str]:
    lo = to_filetime(start) if start is not None else None
    hi = to_filetime(end) if end is not None else None
    if lo is not None and hi is not None and hi < lo:
        raise ValueError('end must be >= start')
    return lo, hi, json.dumps({'lo': lo, 'hi': hi, 'tags': sorted(tags) if tags is not None else None}, sort_keys=True)


def sync_postgres_archive_connection(
    lgh_files: Iterable[str | Path], connection: Any, *,
    start: Any = None, end: Any = None, tags: set[str] | None = None,
    batch_size: int = 1000,
) -> dict[str, int]:
    """Import immutable LGH files into a PostgreSQL DB-API connection.

    Each file's observations and its checksum checkpoint commit atomically.
    Reimports with identical checksum + query are skipped. A changed source
    file or altered query raises, to prevent silent loss. Source identity is
    its resolved path; renaming a file changes that identity.

    Returned counts are *processed* observations, not inserted-row counts.
    This helper commits/rolls back the caller's connection; do not nest it in
    a caller transaction. No concurrent importers are supported.
    """
    if batch_size <= 0:
        raise ValueError('batch_size must be positive')
    if getattr(connection, 'autocommit', False):
        raise ValueError('PostgreSQL archive sync requires autocommit=False')
    lo, hi, key = _query_key(start=start, end=end, tags=tags)
    result: dict[str, int] = {}
    with connection.cursor() as cur:
        try:
            cur.execute(CREATE_RECORDS)
            cur.execute("ALTER TABLE lgh_records ADD COLUMN IF NOT EXISTS value_bits_hex TEXT")
            cur.execute(CREATE_IMPORTS)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    for raw_path in lgh_files:
        path = Path(raw_path).resolve()
        name = str(path)
        before = _digest(path)
        with connection.cursor() as cur:
            try:
                cur.execute('SELECT file_sha256,query_key,record_count FROM lgh_imports WHERE source_file=%s', (name,))
                previous = cur.fetchone()
                if previous is not None:
                    if previous[0] != before:
                        raise FormatError(f'Previously imported archive changed: {name}')
                    if previous[1] != key:
                        raise ValueError(f'Archive previously imported with different filters: {name}; use another database')
                    # Validate a second time to detect modifications during checks.
                    if _digest(path) != before:
                        raise FormatError(f'Archive changed during verification: {name}')
                    connection.commit()
                    result[name] = 0
                    continue

                count = 0
                pending: list[tuple] = []
                reader = LGHReader(path)
                for rec in filter_records(reader.records(tags=tags), start=lo, end=hi):
                    validate_export_record(rec, sink="postgresql")
                    pending.append((name, rec.record_offset, rec.tag, rec.value_type,
                                    rec.value, rec.filetime_ticks, rec.timestamp_iso,
                                    rec.block_offset, rec.record_index, f"{rec.value_bits:016x}"))
                    count += 1
                    if len(pending) >= batch_size:
                        cur.executemany(INSERT_RECORDS, pending)
                        pending.clear()
                if pending:
                    cur.executemany(INSERT_RECORDS, pending)
                if _digest(path) != before:
                    raise FormatError(f'Archive changed during import: {name}')
                cur.execute(INSERT_CHECKPOINT, (name, before, key, count))
                connection.commit()
                result[name] = count
            except BaseException:
                connection.rollback()
                raise
    return result


def sync_postgres_archive(lgh_files: Iterable[str | Path], dsn: str, *,
                          start: Any = None, end: Any = None, tags: set[str] | None = None,
                          batch_size: int = 1000) -> dict[str, int]:
    """Convenience connector for psycopg 3; server integration is optional."""
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL requires 'pip install .[postgres]'") from exc
    with psycopg.connect(dsn, autocommit=False) as conn:
        return sync_postgres_archive_connection(lgh_files, conn, start=start,
                                                end=end, tags=tags, batch_size=batch_size)
