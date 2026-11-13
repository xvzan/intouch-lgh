$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

python -m pip install -e ".[test-arrow,interop]"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -c "import pyarrow, pandas, polars, duckdb; print('PyArrow:', pyarrow.__version__); print('Pandas:', pandas.__version__); print('Polars:', polars.__version__); print('DuckDB:', duckdb.__version__)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python .\scripts\verify_interop.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
