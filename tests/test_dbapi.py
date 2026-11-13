from intouch_lgh.reader import LGHReader
"""DB-API integration, tested using actual fixture records and a connection adapter."""
from pathlib import Path
import sqlite3
import pytest
from examples.legacy_adapters import write_dbapi, write_postgres

FIXTURES = Path(__file__).parent / 'fixtures'


def test_sqlite_dbapi_real_samples_and_idempotence(tmp_path):
    conn = sqlite3.connect(tmp_path / 'db.sqlite')
    try:
        records = lambda: LGHReader(FIXTURES / '24072200.lgh').records()
        assert write_dbapi(records(), conn, batch_size=19) == 412
        assert write_dbapi(records(), conn, batch_size=71, on_conflict="ignore") == 412
        assert conn.execute('SELECT count(*) FROM lgh_records').fetchone()[0] == 412
        assert conn.execute('SELECT count(DISTINCT tag) FROM lgh_records').fetchone()[0] > 0
    finally:
        conn.close()


def test_rollback_failed_source(tmp_path):
    conn = sqlite3.connect(tmp_path / 'db.sqlite')
    raw = bytearray((FIXTURES / '24072200.lgh').read_bytes())
    raw[-512:-508] = b'BAD!'
    broken = tmp_path / 'bad.lgh'
    broken.write_bytes(raw)
    try:
        with pytest.raises(ValueError):
            write_dbapi(LGHReader(broken).records(), conn, batch_size=3)
        assert conn.execute('SELECT count(*) FROM lgh_records').fetchone()[0] == 0
        assert write_dbapi(LGHReader(FIXTURES / '24072200.lgh').records(), conn) == 412
    finally:
        conn.close()


def test_validation_and_no_driver():
    c = sqlite3.connect(':memory:')
    try:
        with pytest.raises(ValueError, match='dialect'):
            write_dbapi([], c, dialect='bogus')
        with pytest.raises(ValueError, match='batch_size'):
            write_dbapi([], c, batch_size=0)
    finally:
        c.close()


class MockPostgresCursor:
    def __init__(self, mock):
        self.mock = mock
    def execute(self, sql):
        assert 'CREATE TABLE' in sql or 'ALTER TABLE' in sql
        self.mock.created = True
    def executemany(self, sql, rows):
        assert '%s' in sql
        self.mock.rows.extend(rows)
    def close(self):
        self.mock.closed = True


class MockPostgres:
    def __init__(self):
        self.rows = []
        self.commits = 0
        self.rollbacks = 0
        self.created = False
        self.closed = False
    def cursor(self):
        return MockPostgresCursor(self)
    def commit(self):
        self.commits += 1
    def rollback(self):
        self.rollbacks += 1


def test_postgres_sql_contract_without_server():
    mock = MockPostgres()
    rows = LGHReader(FIXTURES / '24072200.lgh').records()
    assert write_dbapi(rows, mock, dialect='postgresql', batch_size=80) == 412
    assert len(mock.rows) == 412
    assert mock.commits == 1 and mock.rollbacks == 0 and mock.closed and mock.created
