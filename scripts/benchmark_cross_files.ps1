param(
    [Parameter(Mandatory=$true)][string[]]$LghPaths,
    [string]$OutputDir = ".\cross-file-validation",
    [int]$Repeats = 3,
    [int]$BatchSize = 16384,
    [int]$RowGroupSize = 65536
)
$ErrorActionPreference = "Stop"
$Arguments = @(
    ".\scripts\benchmark_cross_files.py"
) + $LghPaths + @(
    "--output-dir", $OutputDir,
    "--repeats", "$Repeats",
    "--batch-size", "$BatchSize",
    "--row-group-size", "$RowGroupSize"
)
& python @Arguments
if ($LASTEXITCODE -ne 0) { throw "Cross-file Parquet validation failed." }
