<#
.SYNOPSIS
    Start the compute (NVIDIA PC "brain") frame-collection browser. Windows port of compute.sh.

.DESCRIPTION
    On first run this bootstraps a virtualenv at .venv-compute from
    compute/requirements.txt; on later runs it just launches. Works from any
    directory. It serves a web UI to browse collected frames; collection into the
    bounded local store starts OFF — click Start in the UI (or set
    CAT_COLLECT_AUTOSTART=1) to begin ingesting the edge stream.

        .\compute.ps1                          # edge defaults to localhost:8000
        .\compute.ps1 catpi.local:8000         # edge as an argument (http:// added if omitted)
        .\compute.ps1 http://catpi.local:8000  # ...or a full URL
        $env:CAT_COLLECT_PORT=9001; .\compute.ps1   # different web port

    A DISTINCT venv dir (.venv-compute) is used so it never clobbers the edge's
    .venv when both tiers are checked out on one dev box (in production they run
    on different hosts). To rebuild it, delete .venv-compute and re-run.

    Env:
      CAT_PI_URL             edge base URL (default http://localhost:8000; the optional 1st arg wins over it)
      CAT_COLLECT_DIR        store root      (default .\data\collection)
      CAT_COLLECT_MAX_BYTES  retention cap   (default 1099511627776 = 1 TiB on this PC)
      CAT_COLLECT_PORT       web port        (default 8001; the edge uses 8000)
      CAT_COLLECT_AUTOSTART  begin collecting at launch (default off; 1/true/yes/on to enable)
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$EdgeUrl
)

$ErrorActionPreference = 'Stop'

# This script lives at the repo root; run everything relative to it.
$Root = $PSScriptRoot
Set-Location $Root

$Venv = Join-Path $Root '.venv-compute'
$Py = Join-Path $Venv 'Scripts\python.exe'

if (-not (Test-Path $Py)) {
    Write-Host "[compute] creating virtualenv at .venv-compute"
    # The compute code uses `str | None` union syntax, which needs Python >= 3.10.
    # The `py` launcher's default can be an older interpreter (e.g. 3.8), so probe
    # candidates and pick the first that reports >= 3.10 rather than blindly using it.
    # Each candidate is [exe, args...]. Probing runs native tools that may write to
    # stderr and exit nonzero (e.g. `py -3.13` when 3.13 isn't installed prints the
    # launcher's help); under -ErrorActionPreference Stop that would be a terminating
    # error, so relax it and swallow stderr for the duration of the probe.
    $bootstrap = $null
    $candidates = @(
        @('py', '-3.13'), @('py', '-3.12'), @('py', '-3.11'), @('py', '-3.10'),
        @('python'), @('py')
    )
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    foreach ($c in $candidates) {
        $exe = $c[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $rest = @($c | Select-Object -Skip 1)
        # Probe prints major*100+minor as a bare integer. No quotes in the -c string:
        # PowerShell strips embedded double-quotes when passing args to native exes.
        $probe = @($rest) + @('-c', 'import sys; print(sys.version_info[0]*100+sys.version_info[1])')
        # Capture fully BEFORE inspecting: piping a live native command into
        # `Select-Object -First` tears the process down early and forces $LASTEXITCODE
        # to -1 even on success.
        $out = & $exe $probe 2>$null
        $code = $LASTEXITCODE
        $ver = "$(@($out)[-1])".Trim()
        if ($code -eq 0 -and $ver -match '^\d+$' -and [int]$ver -ge 310) { $bootstrap = $c; break }
    }
    $ErrorActionPreference = $prevEap
    if (-not $bootstrap) {
        throw "no Python >= 3.10 found (the compute code needs 3.10+ for str | None syntax). Install it, or point py/python at a 3.10+ interpreter."
    }
    Write-Host "[compute] using interpreter: $($bootstrap -join ' ')"
    $bpRest = @($bootstrap | Select-Object -Skip 1)
    & $bootstrap[0] (@($bpRest) + @('-m', 'venv', $Venv))
    if ($LASTEXITCODE -ne 0) { throw "failed to create virtualenv" }
    & $Py -m pip install --upgrade pip | Out-Null
    & $Py -m pip install -r (Join-Path $Root 'compute\requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "failed to install compute/requirements.txt" }
}

$Port = if ($env:CAT_COLLECT_PORT) { $env:CAT_COLLECT_PORT } else { '8001' }

# The edge the collector connects to, in precedence order: the optional 1st
# positional arg, then the CAT_PI_URL env var, then the edge on THIS host. A bare
# host[:port] with no scheme gets http:// prepended so `.\compute.ps1 catpi.local:8000`
# just works (EdgeClient needs a scheme).
$PiUrl = $EdgeUrl
if (-not $PiUrl) { $PiUrl = $env:CAT_PI_URL }
if (-not $PiUrl) { $PiUrl = 'http://localhost:8000' }
if ($PiUrl -notmatch '://') { $PiUrl = "http://$PiUrl" }
$env:CAT_PI_URL = $PiUrl

$StoreDir = if ($env:CAT_COLLECT_DIR) { $env:CAT_COLLECT_DIR } else { '.\data\collection' }
# This PC has ample disk, so default the retention cap to 1 TiB (vs. the app's
# 5 GiB default) — set and EXPORT it so the app actually uses it. The env var,
# when set by the caller, still wins.
if (-not $env:CAT_COLLECT_MAX_BYTES) { $env:CAT_COLLECT_MAX_BYTES = '1099511627776' }
$MaxBytes = $env:CAT_COLLECT_MAX_BYTES

Write-Host "[compute] edge stream: $PiUrl"
Write-Host "[compute] store:       $StoreDir  (cap $MaxBytes bytes)"
Write-Host "[compute] browse UI:   http://localhost:$Port   (Ctrl-C to stop)"

# --factory: create_app() builds the store and wires the collector (which stays
# stopped until Started from the UI, unless CAT_COLLECT_AUTOSTART is set); there
# is no module-level app that would start a thread on import.
& $Py -m uvicorn --factory compute.api.app:create_app --host 0.0.0.0 --port $Port
exit $LASTEXITCODE
