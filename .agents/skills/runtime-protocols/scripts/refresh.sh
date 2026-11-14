#!/usr/bin/env bash
# Regenerate protocol/ from the locally installed CLIs.
# Local commands only (version, help, schema export): no model requests.
set -euo pipefail

cd "$(dirname "$0")/../../../.."
if [ ! -f AGENTS.md ]; then
  echo "refresh: repository root not found (no AGENTS.md in $(pwd))" >&2
  exit 2
fi

for cli in codex claude; do
  if ! command -v "$cli" >/dev/null; then
    echo "refresh: $cli not found on PATH" >&2
    exit 2
  fi
done

# Build the snapshot aside and swap it in, so a failed run leaves protocol/ untouched.
out=protocol
tmp="$out.tmp"
trap 'rm -rf "$tmp"' EXIT
rm -rf "$tmp"
mkdir -p "$tmp/codex/schema" "$tmp/claude"

codex --version >"$tmp/codex/VERSION"
codex --help >"$tmp/codex/help.txt"
codex exec --help >"$tmp/codex/exec-help.txt"
codex app-server --help >"$tmp/codex/app-server-help.txt"
codex app-server generate-json-schema --out "$tmp/codex/schema"

claude --version >"$tmp/claude/VERSION"
claude --help >"$tmp/claude/help.txt"

rm -rf "$out"
mv "$tmp" "$out"

echo "refresh: wrote $out/ for:"
cat "$out/codex/VERSION" "$out/claude/VERSION"
echo "refresh: review 'git diff $out/', then update the fact tables in references/"
