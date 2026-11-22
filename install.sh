#!/bin/bash
# Install the manager skill for Claude Code on macOS or Linux, then prepare the machine.
set -e
here=$(cd "$(dirname "$0")" && pwd)
dest="$HOME/.claude/skills/manager"

mkdir -p "$dest"
cp -R "$here/manager/." "$dest/"
chmod +x "$dest/scripts/"*.sh "$dest/scripts/orch.py" 2>/dev/null || true
echo "Skill installed: $dest"
echo

bash "$dest/scripts/setup.sh" --install
