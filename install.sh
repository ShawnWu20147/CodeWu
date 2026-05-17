#!/usr/bin/env bash
# install.sh — install CodeWu in editable mode from this repo (POSIX).
#
# Usage:
#   ./install.sh
#   (or: bash install.sh)

set -euo pipefail

# Always operate from this script's directory.
cd "$(dirname "$0")"

printf '\n  CodeWu installer\n  ================\n\n'

# --- Python detection ------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    printf '[!] Python not found on PATH. Install Python 3.10+ first.\n'
    exit 1
fi
printf '  Python:  %s\n' "$(command -v "$PY")"
printf '           %s\n\n' "$($PY --version 2>&1)"

# --- pip install -----------------------------------------------------------
printf '[*] pip install -e .\n'
if ! "$PY" -m pip install -e . ; then
    printf '\n[!] pip install failed.\n'
    printf '    Common causes:\n'
    printf '      - Wrong Python (need >=3.10)\n'
    printf '      - No write permission on site-packages (try `pip install --user -e .` or use a venv)\n'
    exit 1
fi

# --- Done ------------------------------------------------------------------
printf '\n[OK] CodeWu installed.\n\n'
printf '  Run anywhere:     codewu\n'
printf '  Resume latest:    codewu --resume\n'
printf '  Bypass approval:  codewu --allow-all\n\n'
printf '  Config file:      %s/.codewu/config.json\n' "$HOME"
printf '  Sessions:         %s/.codewu/sessions/\n' "$HOME"
printf '  Inside CodeWu:    /help, /config, /sessions\n\n'
