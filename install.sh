#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE="${HOSAKA_REPO:-$(cd "$ROOT/../Hosaka" && pwd)}"
PREFIX="${HOSAKA_PREFIX:-$HOME/.local}"
PYTHON="${PYTHON:-$CORE/.hosakavenv/bin/python}"
[[ -x "$PYTHON" ]] || PYTHON="${CORE}/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="python3"

ARGS=(--core "$CORE" --prefix "$PREFIX")
[[ ! -d "$ROOT/../hosaka_citations" ]] || ARGS+=(--citations "$(cd "$ROOT/../hosaka_citations" && pwd)")
[[ ! -d "$ROOT/../virgo_zone" ]] || ARGS+=(--virgozone "$(cd "$ROOT/../virgo_zone" && pwd)")

exec "$PYTHON" "$ROOT/install.py" "${ARGS[@]}" "$@"
