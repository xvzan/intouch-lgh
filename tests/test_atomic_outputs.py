from intouch_lgh.reader import LGHReader
"""Failure injection: never replace a complete export with a partial one."""
from pathlib import Path
import json
import sqlite3
import pytest
from intouch_lgh import FormatError
from examples.legacy_adapters import write_jsonl, write_sqlite, write_parquet

FIXTURES = Path(__file__).parent / 'fixtures'


def _broken(tmp_path):
    raw = bytearray((FIXTURES / '24072200.lgh').read_bytes())
    raw[-512:-508] = b'BAD!'
    file = tmp_path / 'broken.lgh'
    file.write_bytes(raw)
    return file


def test_jsonl_export_atomic_on_late_parse_failure(tmp_path):
    target = tmp_path / 'history.jsonl'
    target.write_text('KEEP', encoding='utf-8')
    with pytest.raises(FormatError):
        write_jsonl(LGHReader(_broken(tmp_path)).records(), target)
    assert target.read_text() == 'KEEP'
    assert not list(tmp_path.glob('*.partial'))
    assert write_jsonl(LGHReader(FIXTURES / '24072200.lgh').records(), target) == 412
    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert len(rows) == 412
    assert all(isinstance(row['filetime_ticks'], int) for row in rows)


def test_sqlite_late_parse_error_rolls_back(tmp_path):
    target = tmp_path / 'history.sqlite'
    assert write_sqlite(LGHReader(FIXTURES / '24072200.lgh').records(), target) == 412
    with pytest.raises(FormatError):
        write_sqlite(LGHReader(_broken(tmp_path)).records(), target, batch_size=5)
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT count(*) FROM lgh_records').fetchone()[0] == 412


def test_parquet_dependency_error_when_unavailable(tmp_path):
    # Real pyarrow integration is a separate optional test; don't fake a pass.
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match='pyarrow|parquet'):
            write_parquet([], tmp_path / 'test.parquet')
    else:
        pytest.skip('pyarrow installed; real integration test covers this case')


def test_parquet_roundtrip_real_records(tmp_path):
    pa = pytest.importorskip('pyarrow')
    import pyarrow.parquet as pq
    dest = tmp_path / 'test.parquet'
    records = list(LGHReader(FIXTURES / '24072200.lgh').records())
    assert write_parquet(records, dest, batch_size=7) == 412
    table = pq.read_table(dest)
    assert table.num_rows == 412
    assert table['filetime_ticks'].to_pylist() == [r.filetime_ticks for r in records]
    assert table['record_offset'].to_pylist() == [r.record_offset for r in records]
    assert table['value'].to_pylist() == [r.value for r in records]
    assert table['tag'].to_pylist() == [r.tag for r in records]


def test_parquet_atomic_on_late_failure(tmp_path):
    pytest.importorskip('pyarrow')
    dest = tmp_path / 'history.parquet'
    assert write_parquet(LGHReader(FIXTURES / '24072200.lgh').records(), dest) == 412
    original = dest.read_bytes()
    with pytest.raises(FormatError):
        write_parquet(LGHReader(_broken(tmp_path)).records(), dest, batch_size=2)
    assert dest.read_bytes() == original
    assert not list(tmp_path.glob('*.partial'))
