#!/usr/bin/env bash
# Expose the skills in .agents/skills to Claude Code through per-skill symlinks.
# Run once after cloning, and again after adding a skill.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .claude/skills

for skill in .agents/skills/*/; do
  [ -d "$skill" ] || continue
  name="$(basename "$skill")"
  link=".claude/skills/$name"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "link-skills: $link exists and is not a symlink; move it away first" >&2
    exit 1
  fi
  ln -sfn "../../.agents/skills/$name" "$link"
  echo "linked $link -> .agents/skills/$name"
done
