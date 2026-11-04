#!/usr/bin/env bash
# AEC Demo launcher (Linux / macOS / Raspberry Pi / Git Bash on Windows)
set -euo pipefail
cd "$(dirname "$0")"

# 依次尝试候选解释器，跳过 Windows 上 python3 的 Microsoft Store 占位程序
pick_python() {
  local cand
  for cand in ".venv/bin/python" "${PYTHON:-}" python3 python; do
    [ -n "$cand" ] || continue
    if command -v "$cand" >/dev/null 2>&1 && \
       "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >/dev/null 2>&1; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  echo "[AEC] No usable Python 3.9+ found." >&2
  echo "[AEC] Install python3, or set PYTHON=/path/to/python" >&2
  exit 1
fi

if ! "$PY" -c "import fastapi, uvicorn, numpy" >/dev/null 2>&1; then
  echo "[AEC] Missing dependencies, installing requirements.txt ..."
  "$PY" -m pip install -r requirements.txt
fi

exec "$PY" run.py "$@"
