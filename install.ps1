# install.ps1 — install CodeWu in editable mode from this repo.
#
# Usage:
#   .\install.ps1
#
# If your execution policy blocks the script:
#   powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"

# Always operate from this script's directory so the user can run it from anywhere.
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "  CodeWu installer" -ForegroundColor Cyan
Write-Host "  ================" -ForegroundColor DarkGray
Write-Host ""

# --- Python detection -------------------------------------------------------
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $pythonCmd) {
    Write-Host "[!] Python not found on PATH. Install Python 3.10+ first." -ForegroundColor Red
    Write-Host "    https://www.python.org/downloads/" -ForegroundColor DarkGray
    exit 1
}
$pyPath = $pythonCmd.Source
$pyVersion = (& $pyPath --version 2>&1) -join " "
Write-Host "  Python:  $pyPath" -ForegroundColor DarkGray
Write-Host "           $pyVersion" -ForegroundColor DarkGray

# --- Detect a running codewu.exe (pip can't overwrite a locked .exe) -------
$running = Get-Process -Name codewu -ErrorAction SilentlyContinue
if ($running) {
    $pids = ($running | ForEach-Object { $_.Id }) -join ", "
    Write-Host ""
    Write-Host "[!] codewu is currently running (PID: $pids)." -ForegroundColor Yellow
    Write-Host "    /exit any active CodeWu sessions and re-run this script," -ForegroundColor Yellow
    Write-Host "    or force-kill them with:  Stop-Process -Name codewu -Force" -ForegroundColor DarkGray
    exit 1
}

# --- pip install ------------------------------------------------------------
Write-Host ""
Write-Host "[*] pip install -e ." -ForegroundColor Cyan
& $pyPath -m pip install -e .
$rc = $LASTEXITCODE
if ($rc -ne 0) {
    Write-Host ""
    Write-Host "[!] pip install failed (exit $rc)." -ForegroundColor Red
    Write-Host "    Common causes:" -ForegroundColor DarkGray
    Write-Host "      - codewu.exe locked: /exit any running session, then retry" -ForegroundColor DarkGray
    Write-Host "      - Wrong Python (need >=3.10):  $pyVersion" -ForegroundColor DarkGray
    Write-Host "      - No write permission on site-packages" -ForegroundColor DarkGray
    exit $rc
}

# --- Done ------------------------------------------------------------------
Write-Host ""
Write-Host "[OK] CodeWu installed." -ForegroundColor Green
Write-Host ""
Write-Host "  Run anywhere:     codewu" -ForegroundColor Cyan
Write-Host "  Resume latest:    codewu --resume" -ForegroundColor DarkGray
Write-Host "  Bypass approval:  codewu --allow-all" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Config file:      $env:USERPROFILE\.codewu\config.json" -ForegroundColor DarkGray
Write-Host "  Sessions:         $env:USERPROFILE\.codewu\sessions\" -ForegroundColor DarkGray
Write-Host "  Inside CodeWu:    /help, /config, /sessions" -ForegroundColor DarkGray
Write-Host ""
