"""Storage-specific representability checks, not validation of LGH numeric data."""
from __future__ import annotations
import math
from intouch_lgh.reader import Record, FormatError

class ExportValueError(FormatError):
    """Target storage cannot represent the observation under the selected policy."""

def validate_export_record(record: Record, *, sink: str = "jsonl", nan_policy: str = "error") -> None:
    """Guard integer range; reject only values unsupported by the specified sink.

    NaN and infinities are valid IEEE-754 REAL payloads, not LGH parse errors.
    ``nan_policy='bits'`` is supported for JSONL/SQLite with explicit metadata.
    """
    if not 0 <= record.filetime_ticks <= 2**63 - 1:
        raise ExportValueError(
            f"FILETIME outside signed int64 export range at "
            f"{record.source_file}:0x{record.record_offset:x}: {record.filetime_ticks}"
        )
    if sink not in {"jsonl", "sqlite", "postgresql", "parquet"}:
        raise ValueError(f"Unknown sink: {sink}")
    if nan_policy not in {"error", "bits"}:
        raise ValueError("nan_policy must be 'error' or 'bits'")
    if sink in {"jsonl", "sqlite"} and math.isnan(record.value) and nan_policy == "error":
        raise ExportValueError(
            f"NaN value at {record.source_file}:0x{record.record_offset:x}; "
            "use nan_policy='bits' for lossless representation"
        )
    if sink == "jsonl" and math.isinf(record.value) and nan_policy == "error":
        raise ExportValueError(
            f"Non-finite Infinity at {record.source_file}:0x{record.record_offset:x}; "
            "use nan_policy='bits' for lossless representation"
        )
