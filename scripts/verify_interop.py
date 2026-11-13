from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = PROJECT_ROOT / "tests" / "test_arrow_interop.py"

# Make the source checkout importable even when this script is launched from
# another working directory or PYTHONPATH is unset.
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

print(f"Python: {sys.version.split()[0]}")
for module in ("pyarrow", "pandas", "polars", "duckdb"):
    mod = __import__(module)
    print(f"{module}: {getattr(mod, '__version__', 'installed')}")

raise SystemExit(pytest.main([
    "-q", "-rs",
    str(TEST_FILE),
]))
