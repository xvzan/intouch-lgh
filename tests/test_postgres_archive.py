"""PostgreSQL-dialect archive tests through a transactional SQL adapter.

No live PostgreSQL server is present. The adapter adapts placeholders and
PostgreSQL types to SQLite, to check end-to-end SQL and rollback behavior.
A real psycopg/PostgreSQL integration test is still necessary before release.
"""
from pathlib import Path
import sqlite3
import shutil
import pytest

from intouch_lgh import FormatError
from examples.legacy_adapters import sync_postgres_archive_connection

FIXTURES = Path(__file__).parent / 'fixtures'


class PgCursorAdapter:
    def __init__(self, cur):
        self.cur = cur
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.cur.close()
    @staticmethod
    def adapt(sql):
        return sql.replace('%s', '?').replace('DOUBLE PRECISION', 'REAL').replace('BIGINT', 'INTEGER').replace('ADD COLUMN IF NOT EXISTS', 'ADD COLUMN')
    def execute(self, sql, args=()):
        if "ADD COLUMN IF NOT EXISTS value_bits_hex" in sql:
            return None  # SQLite test adapter: CREATE already includes the column
        return self.cur.execute(self.adapt(sql), args)
    def executemany(self, sql, args):
        return self.cur.executemany(self.adapt(sql), args)
    def fetchone(self):
        return self.cur.fetchone()


class PgConnectionAdapter:
    autocommit = False
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.commits = 0
        self.rollbacks = 0
    def cursor(self):
        return PgCursorAdapter(self.db.cursor())
    def commit(self):
        self.db.commit()
        self.commits += 1
    def rollback(self):
        self.db.rollback()
        self.rollbacks += 1
    def scalar(self, sql):
        return self.db.execute(sql).fetchone()[0]
    def close(self):
        self.db.close()


def test_two_archives_repeat_and_exact_records():
    conn = PgConnectionAdapter()
    try:
        files = [FIXTURES / '24072200.lgh', FIXTURES / '24072300.lgh']
        assert list(sync_postgres_archive_connection(files, conn, batch_size=17).values()) == [412, 4523]
        assert conn.scalar('SELECT count(*) FROM lgh_records') == 4935
        assert conn.scalar('SELECT count(*) FROM lgh_imports') == 2
        assert list(sync_postgres_archive_connection(files, conn).values()) == [0, 0]
        assert conn.scalar('SELECT count(*) FROM lgh_records') == 4935
        assert conn.commits >= 6
    finally:
        conn.close()


def test_corrupted_later_file_rolls_back_and_retry(tmp_path):
    good = FIXTURES / '24072200.lgh'
    second = tmp_path / 'later.lgh'
    shutil.copyfile(FIXTURES / '24072300.lgh', second)
    raw = bytearray(second.read_bytes())
    raw[-512:-508] = b'BAD!'
    second.write_bytes(raw)
    conn = PgConnectionAdapter()
    try:
        with pytest.raises(FormatError):
            sync_postgres_archive_connection([good, second], conn, batch_size=5)
        assert conn.scalar('SELECT count(*) FROM lgh_records') == 412
        assert conn.scalar('SELECT count(*) FROM lgh_imports') == 1
        assert conn.rollbacks >= 1
        shutil.copyfile(FIXTURES / '24072300.lgh', second)
        assert list(sync_postgres_archive_connection([good, second], conn).values()) == [0, 4523]
        assert conn.scalar('SELECT count(*) FROM lgh_records') == 4935
    finally:
        conn.close()


def test_source_changes_or_query_changes_rejected(tmp_path):
    p = tmp_path / 'copy.lgh'
    shutil.copyfile(FIXTURES / '24072200.lgh', p)
    conn = PgConnectionAdapter()
    try:
        assert list(sync_postgres_archive_connection([p], conn, tags={'MY_REAL'}).values()) == [0]
        with pytest.raises(ValueError, match='different filters'):
            sync_postgres_archive_connection([p], conn)
        with p.open('ab') as f:
            f.write(b'X')
        with pytest.raises(FormatError, match='changed'):
            sync_postgres_archive_connection([p], conn, tags={'MY_REAL'})
        assert conn.scalar('SELECT count(*) FROM lgh_imports') == 1
    finally:
        conn.close()


def test_invalid_config_does_not_touch_database():
    conn = PgConnectionAdapter()
    try:
        with pytest.raises(ValueError, match='batch_size'):
            sync_postgres_archive_connection([], conn, batch_size=0)
        with pytest.raises(ValueError, match='end must'):
            sync_postgres_archive_connection([], conn, start=12, end=11)
        conn.autocommit = True
        with pytest.raises(ValueError, match='autocommit'):
            sync_postgres_archive_connection([], conn)
    finally:
        conn.close()
