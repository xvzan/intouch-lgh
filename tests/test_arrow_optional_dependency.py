from __future__ import annotations

import importlib.util
import pytest
from intouch_lgh import __version__


def test_arrow_adapter_import_is_lazy_and_reports_missing_extra():
    from intouch_lgh.arrow_adapter import to_arrow_batch
    from intouch_lgh import RawBatch

    if importlib.util.find_spec("pyarrow") is not None:
        pytest.skip("PyArrow is installed; integration tests cover this path")
    raw = RawBatch(b"", b"", b"", (), "empty.lgh")
    with pytest.raises(ImportError, match=r"intouch-lgh\[arrow\]"):
        to_arrow_batch(raw)


def test_core_import_does_not_load_arrow_or_numpy():
    code = """
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'numpy' or name.startswith('numpy.'):
        raise AssertionError('numpy imported')
    if name == 'pyarrow' or name.startswith('pyarrow.'):
        raise AssertionError('pyarrow imported')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import intouch_lgh
from intouch_lgh.arrow_adapter import to_arrow_batch
from intouch_lgh.parquet_adapter import export_parquet
assert callable(to_arrow_batch)
assert callable(export_parquet)
print(intouch_lgh.__version__)
"""
    import subprocess, sys
    from pathlib import Path
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == __version__



def test_arrow_column_projection_normalization_accepts_string_and_generator():
    from intouch_lgh.arrow_adapter import _normalize_columns

    assert _normalize_columns("value") == ("value",)
    generated = (name for name in ("filetime_ticks", "value_bits"))
    assert _normalize_columns(generated) == ("filetime_ticks", "value_bits")


def test_arrow_column_projection_rejects_empty_and_duplicate_columns():
    from intouch_lgh.arrow_adapter import _normalize_columns

    with pytest.raises(ValueError, match="at least one"):
        _normalize_columns(())
    with pytest.raises(ValueError, match="duplicates"):
        _normalize_columns(("value", "value"))
