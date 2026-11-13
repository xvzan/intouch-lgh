param(
    [Parameter(Mandatory=$true)][string]$LghPath,
    [string]$OutputDir = ".\small-batch-validation",
    [int]$Repeats = 5,
    [int[]]$BatchSizes = @(4096, 8192, 16384),
    [int]$RowGroupSize = 65536
)
$ErrorActionPreference = "Stop"
$Arguments = @(
    $LghPath,
    "--output-dir", $OutputDir,
    "--repeats", "$Repeats",
    "--batch-sizes"
) + $BatchSizes + @(
    "--row-group-sizes", "$RowGroupSize",
    "--export-only"
)
& python .\scripts\benchmark_parquet_validation.py @Arguments
if ($LASTEXITCODE -ne 0) { throw "Small-Batch Parquet benchmark failed." }
