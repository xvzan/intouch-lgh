"""Atomic, resumable SQLite import for immutable/archived LGH files ONLY.

This intentionally does not implement tailing currently-written LGH files.
A file is fingerprinted before and after parsing and each completed import is
committed atomically with its checkpoint. Changed archives are rejected.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import sqlite3
from intouch_lgh.reader import LGHReader, FormatError
from .validation import validate_export_record
from intouch_lgh.query import filter_records


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def sync_sqlite_archive(lgh_files, destination: str | Path, *, start=None, end=None,
                        tags: set[str] | None = None) -> dict[str, int]:
    """Import immutable LGH archives in one transaction per file.

    One checkpoint is associated with each resolved source path and a fixed
    query definition. A repeated identical import is skipped; a modified file
    or a different query is rejected, avoiding silent data loss.

    Returns numbers of newly processed rows by file; not database insert counts.
    Partial import failures roll back the records AND the checkpoint.
    """
    from intouch_lgh.query import to_filetime
    import json
    lo = to_filetime(start) if start is not None else None
    hi = to_filetime(end) if end is not None else None
    if lo is not None and hi is not None and hi < lo:
        raise ValueError('end must be >= start')
    query_key = json.dumps({'lo': lo, 'hi': hi, 'tags': sorted(tags) if tags is not None else None}, sort_keys=True)
    conn = sqlite3.connect(str(destination))
    result = {}
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS lgh_records (
            source_file TEXT NOT NULL, record_offset INTEGER NOT NULL,
            tag TEXT NOT NULL, value_type TEXT NOT NULL, value REAL,
            filetime_ticks INTEGER NOT NULL, timestamp_utc TEXT NOT NULL,
            block_offset INTEGER NOT NULL, record_index INTEGER NOT NULL,
            PRIMARY KEY(source_file, record_offset))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS lgh_imports (
            source_file TEXT NOT NULL PRIMARY KEY, file_sha256 TEXT NOT NULL,
            query_key TEXT NOT NULL, record_count INTEGER NOT NULL)''')
        conn.commit()
        for raw_path in lgh_files:
            path = Path(raw_path).resolve()
            name = str(path)
            before = _digest(path)
            previous = conn.execute('SELECT file_sha256, query_key, record_count FROM lgh_imports WHERE source_file=?', (name,)).fetchone()
            if previous is not None:
                if previous[0] != before:
                    raise FormatError(f'Previously imported archive changed: {name}')
                if previous[1] != query_key:
                    raise ValueError(f'Archive previously imported with different filters: {name}; use another database')
                result[name] = 0
                continue
            count = 0
            try:
                with conn:
                    reader = LGHReader(path)
                    records = filter_records(reader.records(tags=tags), start=lo, end=hi)
                    for r in records:
                        validate_export_record(r, sink="sqlite")
                        conn.execute('''INSERT INTO lgh_records
                            (source_file,record_offset,tag,value_type,value,filetime_ticks,timestamp_utc,block_offset,record_index)
                            VALUES (?,?,?,?,?,?,?,?,?)''',
                            (name, r.record_offset, r.tag, r.value_type, r.value,
                             r.filetime_ticks, r.timestamp_iso, r.block_offset, r.record_index))
                        count += 1
                    after = _digest(path)
                    if after != before:
                        raise FormatError(f'Archive changed during import: {name}')
                    conn.execute('INSERT INTO lgh_imports VALUES (?,?,?,?)', (name, before, query_key, count))
            except Exception:
                # The context manager rolls back both rows and checkpoint.
                raise
            result[name] = count
        return result
    finally:
        conn.close()
