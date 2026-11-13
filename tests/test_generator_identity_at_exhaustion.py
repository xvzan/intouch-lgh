"""Regression checks: source identity remains enforced after generator start."""
from pathlib import Path
import shutil
import pytest
from intouch_lgh import Archive, ArchiveChangedError

FIXTURES = Path(__file__).parent / 'fixtures'

@pytest.mark.parametrize('kind', ['records', 'batches'])
def test_detects_source_change_after_iteration_started(tmp_path, kind):
    lgh=tmp_path/'24072300.lgh'
    idx=tmp_path/'24072300.idx'
    shutil.copyfile(FIXTURES/'24072300.lgh',lgh)
    shutil.copyfile(FIXTURES/'24072300.idx',idx)
    with Archive.open(lgh,idx,validate='none') as archive:
        it=(archive.iter_records(tags='MY_REAL') if kind=='records'
            else archive.iter_batches(tags='MY_REAL',batch_size=16))
        first=next(it)
        assert first is not None
        with lgh.open('ab') as out:
            out.write(b'append invalid data')
        with pytest.raises(ArchiveChangedError):
            list(it)

@pytest.mark.parametrize('kind', ['records', 'batches'])
def test_normal_exhaustion_accepts_unchanged_source(kind):
    lgh=FIXTURES/'24072300.lgh'
    idx=lgh.with_suffix('.idx')
    with Archive.open(lgh,idx,validate='none') as archive:
        assert list(archive.iter_records(tags='MY_REAL') if kind=='records'
                    else archive.iter_batches(tags='MY_REAL',batch_size=16))
