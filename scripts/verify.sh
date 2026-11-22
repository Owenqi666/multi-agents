#!/usr/bin/env bash
# Run the project's lint, format, type, unit, and replay checks.
# Usage: scripts/verify.sh [--list] [step ...]   (no step = every step, in order)
set -euo pipefail

steps=(lint format typecheck unit replay)

run_step() {
  case "$1" in
    lint) uv run ruff check . ;;
    format) uv run ruff format --check . ;;
    typecheck) uv run mypy src ;;
    unit) uv run pytest tests/unit -q ;;
    replay) uv run pytest tests/replay -q ;;
    *) return 2 ;;
  esac
}

is_step() {
  local known
  for known in "${steps[@]}"; do
    [ "$known" = "$1" ] && return 0
  done
  return 1
}

if [ "${1:-}" = "--list" ]; then
  printf '%s\n' "${steps[@]}"
  exit 0
fi

[ "$#" -gt 0 ] || set -- "${steps[@]}"

for step in "$@"; do
  if ! is_step "$step"; then
    echo "verify: unknown step '$step' (known: ${steps[*]})" >&2
    exit 2
  fi
done

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null; then
  echo "verify: uv not found on PATH" >&2
  exit 2
fi
if [ ! -f pyproject.toml ]; then
  echo "verify: pyproject.toml not found; scaffold the project first" >&2
  exit 2
fi

for step in "$@"; do
  echo "== verify: $step"
  if ! run_step "$step"; then
    echo "== verify: FAILED at '$step'" >&2
    exit 1
  fi
done
echo "== verify: OK ($*)"
