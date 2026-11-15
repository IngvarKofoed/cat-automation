# compute.ps1 ports the compute launcher to Windows

<!-- entry 18 -->

Added `compute.ps1`, a Windows PowerShell port of `compute.sh` — the compute tier's
real home is the NVIDIA PC, which here runs Windows. Same behavior: bootstraps
`.venv-compute`, resolves the edge URL (arg > CAT_PI_URL > localhost:8000, scheme
auto-prepended), launches the collector UI via uvicorn. Probes for a Python >= 3.10
interpreter (the compute code uses `str | None` unions) rather than trusting the
`py` launcher's default, which can be an older 3.8.
