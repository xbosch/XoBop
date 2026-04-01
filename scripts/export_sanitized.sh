#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTDIR="${1:-$ROOT/../exports}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$OUTDIR/xobops-dev-clean-sanitized-$STAMP.zip"

mkdir -p "$OUTDIR"
cd "$ROOT"

zip -r "$OUT" . \
  -x '.git/*' \
     '.venv/*' \
     '.env' \
     'memory.db' 'memory.db-*' \
     'backups/*' \
     'artifacts/*' \
     'state/*' \
     'memory/*' \
     'MEMORY.md' \
     'config/self_mods.json' \
     '*/__pycache__/*' '__pycache__/*' '*.pyc' \
     '.pytest_cache/*'

echo "$OUT"
