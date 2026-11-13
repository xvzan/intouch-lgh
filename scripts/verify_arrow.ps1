$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

python -m pip install -e ".[test-arrow]"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -c "import sys, numpy, pyarrow; print('Python:', sys.version); print('NumPy:', numpy.__version__); print('PyArrow:', pyarrow.__version__)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -m pytest -q -rs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
