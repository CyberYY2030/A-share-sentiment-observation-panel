$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutputDir = Join-Path $Root "output\backfill_jobs"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$Candidates = @(
    @{ Exe = (Join-Path $Root ".venv\Scripts\python.exe"); Args = @() },
    @{ Exe = "py"; Args = @("-3") },
    @{ Exe = "python"; Args = @() }
)
$PythonExe = $null
$PythonArgs = @()
foreach ($Candidate in $Candidates) {
    if (($Candidate.Exe -like "*\*") -and -not (Test-Path -LiteralPath $Candidate.Exe)) {
        continue
    }
    try {
        & $Candidate.Exe @($Candidate.Args) --version *> $null
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = $Candidate.Exe
            $PythonArgs = @($Candidate.Args)
            break
        }
    } catch {
        continue
    }
}
if (-not $PythonExe) {
    throw "No usable Python interpreter found. Checked .venv, py -3, and python."
}

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogPath = Join-Path $OutputDir "daily_job_$Stamp.log"
$Script = Join-Path $Root "offline_daily_update.py"

& $PythonExe @PythonArgs $Script --base-dir $Root @args *> $LogPath
$ExitCode = $LASTEXITCODE
Get-Content -LiteralPath $LogPath
exit $ExitCode
