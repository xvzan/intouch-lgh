# Run from any working directory. Each requested Python version gets a clean venv.
param(
    [string[]]$Versions = @('3.10', '3.12'),
    [switch]$WithInterop
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

# Test the redistributable public fixtures, independently of any local corpus.
Remove-Item Env:INTOUCH_LGH_CORPUS_DIR -ErrorAction SilentlyContinue
Remove-Item Env:INTOUCH_LGH_EXTRA_FIXTURES -ErrorAction SilentlyContinue

foreach ($version in $Versions) {
    $venv = Join-Path $projectRoot ('.venv-py' + $version.Replace('.', ''))
    Write-Host ('===== Python ' + $version + ' =====')
    & py ('-' + $version) --version
    if ($LASTEXITCODE -ne 0) { throw "Python $version is unavailable; install it or adjust -Versions." }
    & py ('-' + $version) -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create venv for Python $version" }
    $python = Join-Path $venv 'Scripts/python.exe'
    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed for Python $version" }
    $extra = if ($WithInterop) { '.[test-arrow,interop]' } else { '.[test]' }
    & $python -m pip install -e $extra
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed for Python $version" }
    & $python -m pytest -q -rs
    if ($LASTEXITCODE -ne 0) { throw "Tests failed for Python $version" }
}
Write-Host 'All selected Python-version test runs passed.'
