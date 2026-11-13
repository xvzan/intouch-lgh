$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Version = python -c "from intouch_lgh import __version__; print(__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "Could not determine intouch-lgh package version."
}

Write-Host "InTouch LGH v$Version full validation"
Write-Host "Python:"
python --version
Write-Host "Optional data stack:"
python -c "import importlib.util; mods=('numpy','pyarrow','pandas','polars','duckdb'); [print(f'{m}: ' + (__import__(m).__version__ if importlib.util.find_spec(m) else 'NOT INSTALLED')) for m in mods]"
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect optional data stack."
}

Write-Host "Running the complete test suite, including streaming Parquet and Arrow interoperability tests."
python -m pytest -q -rs
if ($LASTEXITCODE -ne 0) {
    throw "Full validation failed for intouch-lgh v$Version."
}
