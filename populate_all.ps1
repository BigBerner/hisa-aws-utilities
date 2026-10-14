[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$sourceDirectory = Join-Path $PSScriptRoot 'src'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python environment not found: $python"
}

$scripts = @(
    Get-ChildItem -LiteralPath $sourceDirectory -Filter 'populate_*.py' -File |
        Sort-Object Name
)
if ($scripts.Count -eq 0) {
    throw "No population scripts found in $sourceDirectory"
}

foreach ($script in $scripts) {
    if ($PSCmdlet.ShouldProcess($script.FullName, 'Run population script (writes to AWS)')) {
        Write-Host "Running $($script.Name)..."
        & $python $script.FullName
        if ($LASTEXITCODE -ne 0) {
            throw "$($script.Name) failed with exit code $LASTEXITCODE. Remaining scripts were not run."
        }
        Write-Host "Completed $($script.Name)"
    }
}
