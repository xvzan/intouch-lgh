"""Read-only access to the currently supported InTouch LGH/IDX archive layout.

The public v1 API is intentionally small. NumPy is available through the
optional ``intouch_lgh.numpy_adapter`` module and is never imported here.
"""
from .archive import Archive, ArchiveChangedError
from .model import RawBatch, RawBlock, ValidationReport
from .reader import Record, FormatError

__version__ = "1.0.2"

__all__ = [
    "Archive",
    "ArchiveChangedError",
    "FormatError",
    "RawBatch",
    "RawBlock",
    "Record",
    "ValidationReport",
    "__version__",
]
