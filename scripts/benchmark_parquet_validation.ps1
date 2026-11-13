param(
    [Parameter(Mandatory=$true)][string]$LghPath,
    [string]$OutputDir = ".\parquet-validation",
    [int]$Repeats = 3,
    [int]$QueryRepeats = 5,
    [int[]]$BatchSizes = @(16384, 65536),
    [int[]]$RowGroupSizes = @(16384, 65536, 262144)
)
$ErrorActionPreference = "Stop"
$Arguments = @(
    $LghPath,
    "--output-dir", $OutputDir,
    "--repeats", "$Repeats",
    "--query-repeats", "$QueryRepeats",
    "--batch-sizes"
) + $BatchSizes + @("--row-group-sizes") + $RowGroupSizes
& python .\scripts\benchmark_parquet_validation.py @Arguments
if ($LASTEXITCODE -ne 0) { throw "Parquet validation benchmark failed." }
