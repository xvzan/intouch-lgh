$ErrorActionPreference = "Stop"
param(
    [Parameter(Mandatory=$true)][string]$LghPath,
    [string]$OutputDir = ".\parquet-matrix"
)
python .\scripts\benchmark_parquet_matrix.py $LghPath --output-dir $OutputDir
if ($LASTEXITCODE -ne 0) { throw "Parquet benchmark failed." }
