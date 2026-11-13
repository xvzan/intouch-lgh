from __future__ import annotations

import importlib.util
import pytest


def test_parquet_adapter_import_is_lazy():
    from intouch_lgh.parquet_adapter import export_parquet
    assert callable(export_parquet)


def test_parquet_export_reports_missing_optional_dependency(tmp_path):
    if importlib.util.find_spec("pyarrow") is not None:
        pytest.skip("PyArrow is installed; Parquet integration tests cover this path")

    from intouch_lgh import RawBatch
    from intouch_lgh.parquet_adapter import export_parquet

    class EmptyArchive:
        lgh_path = tmp_path / "empty.lgh"

        def iter_batches(self, **kwargs):
            return iter(())

    with pytest.raises(ImportError, match=r"intouch-lgh\[parquet\]"):
        export_parquet(EmptyArchive(), tmp_path / "out.parquet")


def test_atomic_publish_helper_never_clobbers_without_overwrite(tmp_path):
    from intouch_lgh.parquet_adapter import _commit_temp

    temp = tmp_path / ".new.partial"
    destination = tmp_path / "final.parquet"
    temp.write_bytes(b"complete-new-file")
    _commit_temp(temp, destination, overwrite=False)
    assert destination.read_bytes() == b"complete-new-file"
    assert not temp.exists()

    replacement = tmp_path / ".replacement.partial"
    replacement.write_bytes(b"replacement")
    with pytest.raises(FileExistsError):
        _commit_temp(replacement, destination, overwrite=False)
    assert destination.read_bytes() == b"complete-new-file"
    assert replacement.exists()

    _commit_temp(replacement, destination, overwrite=True)
    assert destination.read_bytes() == b"replacement"
    assert not replacement.exists()
