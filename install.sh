#!/bin/bash
# Install or update the manager skill for Claude Code on macOS or Linux.
#
#   install.sh           copy the skill, then install what the machine lacks
#   install.sh --update  copy the skill only and report what is present (installs nothing)
set -e
here=$(cd "$(dirname "$0")" && pwd)
dest="$HOME/.claude/skills/manager"
mode=install
[ "${1:-}" = "--update" ] && mode=update

version=$(git -C "$here" log -1 --format='%h %cs' 2>/dev/null || true)
[ -n "$version" ] || version="unknown $(date +%Y-%m-%d)"
old=$(cat "$dest/VERSION" 2>/dev/null || echo "not installed")

# Build the new copy next to the old one and swap, so files removed from the repository do not linger.
new="$dest.new.$$"
rm -rf "$new"
mkdir -p "$(dirname "$dest")"
cp -R "$here/manager" "$new"
chmod +x "$new/scripts/"*.sh "$new/scripts/orch.py" 2>/dev/null || true
echo "$version" > "$new/VERSION"
rm -rf "$dest"
mv "$new" "$dest"
echo "Skill installed: $dest"
echo "Version: $old -> $version"
echo

if [ "$mode" = "update" ]; then
    bash "$dest/scripts/setup.sh"
else
    bash "$dest/scripts/setup.sh" --install
fi
