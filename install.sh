#!/bin/bash
# Install or update the manager skill for Claude Code on macOS or Linux.
#
#   install.sh            copy the skill, then install what the machine lacks
#   install.sh --update   copy the skill only and report what is present (installs nothing)
#   --no-hook             with either: do not add the audit hook to ~/.claude/settings.json
set -e
here=$(cd "$(dirname "$0")" && pwd)
dest="$HOME/.claude/skills/manager"
mode=install
hook=1
for arg in "$@"; do
    case "$arg" in
        --update) mode=update ;;
        --no-hook) hook=0 ;;
        *) echo "unknown option: $arg"; exit 1 ;;
    esac
done

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

status=0
if [ "$mode" = "update" ]; then
    bash "$dest/scripts/setup.sh" || status=$?
else
    bash "$dest/scripts/setup.sh" --install || status=$?
fi

# The hook that refuses commit and push while a manager job is open and unaudited.
if [ "$hook" = "1" ]; then
    echo
    export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
    python3 "$dest/scripts/orch.py" hook-install \
        || echo "[you]     the audit hook was not installed; once Python 3 is there run: python3 $dest/scripts/orch.py hook-install"
fi
exit $status
