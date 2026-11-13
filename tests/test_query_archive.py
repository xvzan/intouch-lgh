from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
import pytest
from intouch_lgh.reader import LGHReader
from intouch_lgh.index import IDXReader
from intouch_lgh.query import filter_records, to_filetime

from intouch_lgh import FormatError
from examples.legacy_adapters import sync_sqlite_archive

FIXTURES = Path(__file__).parent / 'fixtures'


def test_100ns_boundaries_and_timezone():
    assert to_filetime('2024-07-23T04:48:18.4240000Z') + 1 == to_filetime('2024-07-23T04:48:18.4240001+00:00')
    assert to_filetime('2024-07-23T12:48:18.4240000+08:00') == to_filetime('2024-07-23T04:48:18.4240000Z')
    assert to_filetime(datetime(2024,7,23,4,48,18,424000,tzinfo=timezone.utc)) == to_filetime('2024-07-23T04:48:18.424Z')
    with pytest.raises(ValueError):
        to_filetime('2024-07-23T04:48:18')
    with pytest.raises(ValueError):
        to_filetime('2024-07-23T04:48:18.42400001Z')
    with pytest.raises(ValueError):
        to_filetime(datetime(2024,7,23))


def test_query_matches_sequential_and_idx_and_boundaries():
    p = FIXTURES / '24072300.lgh'
    all_records = list(LGHReader(p).records())
    tag = 'MY_REAL'
    chosen = [r for r in all_records if r.tag == tag]
    lo = chosen[15].filetime_ticks
    hi = chosen[42].filetime_ticks
    want = [r for r in chosen if lo <= r.filetime_ticks < hi]
    seq = list(filter_records(LGHReader(p).records(tags={tag}), start=lo, end=hi))
    indexed = list(filter_records(IDXReader(p.with_suffix('.idx')).records_for_tag(p, tag), start=lo, end=hi))
    assert [r.record_offset for r in seq] == [r.record_offset for r in indexed] == [r.record_offset for r in want]
    assert all(r.filetime_ticks < hi for r in seq)
    assert list(filter_records(all_records, start=lo, end=lo)) == []
    with pytest.raises(ValueError):
        list(filter_records([], start=hi, end=lo))


def test_archive_idempotence_two_files(tmp_path):
    files = [FIXTURES / '24072200.lgh', FIXTURES / '24072300.lgh']
    db = tmp_path / 'archive.db'
    first = sync_sqlite_archive(files, db)
    assert list(first.values()) == [412, 4523]
    second = sync_sqlite_archive(files, db)
    assert list(second.values()) == [0, 0]
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from lgh_records').fetchone()[0] == 4935
        assert c.execute('select count(*) from lgh_imports').fetchone()[0] == 2
    with pytest.raises(ValueError, match='different filters'):
        sync_sqlite_archive(files, db, tags={'MY_REAL'})


def test_archive_modification_rejected(tmp_path):
    p = tmp_path/'changed.lgh'
    shutil.copyfile(FIXTURES/'24072200.lgh',p)
    db = tmp_path/'out.db'
    assert list(sync_sqlite_archive([p], db).values()) == [412]
    with p.open('ab') as f:
        f.write(b'X')
    with pytest.raises(FormatError, match='changed'):
        sync_sqlite_archive([p], db)
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from lgh_records').fetchone()[0] == 412


def test_archive_corruption_rolls_back(tmp_path):
    p = tmp_path/'bad.lgh'
    raw = bytearray((FIXTURES/'24072200.lgh').read_bytes())
    raw[-512:-508] = b'BAD!'
    p.write_bytes(raw)
    db = tmp_path/'out.db'
    with pytest.raises(FormatError):
        sync_sqlite_archive([p],db)
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from lgh_records').fetchone()[0] == 0
        assert c.execute('select count(*) from lgh_imports').fetchone()[0] == 0


def test_filter_archived_import(tmp_path):
    p = FIXTURES/'24072300.lgh'
    r = list(LGHReader(p).records())
    bound = r[len(r)//2].filetime_ticks
    want = len(list(filter_records(r,start=bound)))
    db = tmp_path/'half.db'
    assert list(sync_sqlite_archive([p],db,start=bound).values()) == [want]
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from lgh_records').fetchone()[0] == want


def test_resume_after_later_archive_fails(tmp_path):
    good = FIXTURES/'24072200.lgh'
    broken = tmp_path/'broken.lgh'
    broken.write_bytes((FIXTURES/'24072300.lgh').read_bytes()[:-1])
    db = tmp_path/'resumable.db'
    with pytest.raises(FormatError):
        sync_sqlite_archive([good, broken], db)
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from lgh_records').fetchone()[0] == 412
        assert c.execute('select count(*) from lgh_imports').fetchone()[0] == 1
    broken.write_bytes((FIXTURES/'24072300.lgh').read_bytes())
    again = sync_sqlite_archive([good, broken], db)
    assert list(again.values()) == [0, 4523]
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from lgh_records').fetchone()[0] == 4935
        assert c.execute('select count(*) from lgh_imports').fetchone()[0] == 2
