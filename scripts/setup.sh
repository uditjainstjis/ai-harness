#!/usr/bin/env bash
# Installs Pramana into ./.venv. Works with or without uv; needs Python >= 3.9 and git.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=""
for cand in python3.12 python3.11 python3.13 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then echo "ERROR: Python >= 3.9 is required." >&2; exit 1; fi
command -v git >/dev/null 2>&1 || { echo "ERROR: git is required." >&2; exit 1; }
echo "==> Python: $("$PY" --version 2>&1) ($(command -v "$PY"))"

install_with_pip() {
  .venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1 || true
  .venv/bin/python -m pip install -q -e ".[dev]" || .venv/bin/python -m pip install -q ".[dev]"
}

if command -v uv >/dev/null 2>&1; then
  echo "==> Creating .venv with uv"
  uv venv -q --allow-existing --python "$PY" .venv
  uv pip install -q --python .venv/bin/python -e ".[dev]"
elif "$PY" -m venv .venv >/dev/null 2>&1; then
  echo "==> Creating .venv with venv"
  install_with_pip
elif "$PY" -m pip install -q --user virtualenv >/dev/null 2>&1 && "$PY" -m virtualenv -q .venv; then
  echo "==> Creating .venv with virtualenv"
  install_with_pip
else
  echo "==> venv unavailable; bootstrapping uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  uv venv -q --python "$PY" .venv
  uv pip install -q --python .venv/bin/python -e ".[dev]"
fi

command -v rg >/dev/null 2>&1 || echo "note: ripgrep (rg) not found; using the built-in Python search (same results, slower)."
command -v ctags >/dev/null 2>&1 || echo "note: universal-ctags not found; using built-in symbol extraction."
.venv/bin/pramana --version >/dev/null && echo "==> Pramana $(.venv/bin/pramana --version) installed."
if [ -z "${AI_API_KEY:-}" ]; then
  echo "==> Next: export AI_API_KEY=<key> && make run"
else
  echo "==> AI_API_KEY detected. Next: make run"
fi
