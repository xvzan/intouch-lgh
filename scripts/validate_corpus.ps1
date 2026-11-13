param(
    [Parameter(Mandatory=$true)][string]$CorpusDir,
    [string[]]$Samples = @(),
    [int]$BatchSize = 65536,
    [string]$Output = ""
)
$ErrorActionPreference = "Stop"
$extraArgs = @()
if ($Samples.Count -gt 0) { $extraArgs = @("--samples") + $Samples }
if ([string]::IsNullOrWhiteSpace($Output)) {
    $Output = Join-Path $CorpusDir "corpus_validation_report.json"
}
& python .\scripts\validate_corpus.py $CorpusDir @extraArgs `
    --batch-size $BatchSize --output $Output
if ($LASTEXITCODE -ne 0) { throw "LGH/IDX corpus validation failed." }
