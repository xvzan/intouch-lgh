"""Archived v0.12 output adapters kept for comparison and regression tests.

These modules are outside the installable intouch_lgh package, deliberately.
They are not a promised public API and are not recommended for new integrations.
"""
from .sinks import write_jsonl, write_sqlite, write_parquet
from .validation import ExportValueError
from .archive import sync_sqlite_archive
from .dbapi import write_dbapi, write_postgres
from .postgres_archive import sync_postgres_archive, sync_postgres_archive_connection
__all__ = ['write_jsonl','write_sqlite','write_parquet','ExportValueError','sync_sqlite_archive','write_dbapi','write_postgres','sync_postgres_archive','sync_postgres_archive_connection']
