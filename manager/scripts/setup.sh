#!/bin/bash
# Prepare a macOS or Linux machine for the manager skill.
#
#   setup.sh            report what is present and what is missing (changes nothing)
#   setup.sh --install  install what can be installed without a password
#
# Exit codes: 0 ready, 1 something is missing (re-run with --install),
#             2 a step only the user can do is pending (printed as [you]).
set -u

INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
OS=$(uname -s)
HERE=$(cd "$(dirname "$0")" && pwd)
missing=0
need_user=0

ok()   { printf '[ok]      %s\n' "$*"; }
miss() { printf '[missing] %s\n' "$*"; missing=1; }
you()  { printf '[you]     %s\n' "$*"; need_user=1; }
info() { printf '          %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# On a bare Mac /usr/bin/python3 is a stub that pops up an installer dialog; do not call it
# unless the Command Line Tools are really there.
python_ok() {
    have python3 || return 1
    if [ "$OS" = "Darwin" ] && [ "$(command -v python3)" = "/usr/bin/python3" ]; then
        xcode-select -p >/dev/null 2>&1 || return 1
    fi
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' >/dev/null 2>&1
}

case "$OS" in
    Darwin|Linux) ;;
    *) echo "This script is for macOS and Linux. On Windows run setup.ps1 instead."; exit 1 ;;
esac

# --- tmux and python3 -------------------------------------------------------------------------
need_pkgs=""
have tmux || need_pkgs="$need_pkgs tmux"
python_ok || need_pkgs="$need_pkgs python"

if [ -n "$need_pkgs" ]; then
    if [ "$OS" = "Darwin" ]; then
        if ! have brew; then
            you "Homebrew is needed to install:$need_pkgs. It asks for your Mac password, so run this yourself in Terminal:"
            info '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            info "Then run this setup again."
        elif [ "$INSTALL" = "1" ]; then
            for p in $need_pkgs; do
                echo "Installing $p with Homebrew..."
                brew install "$p" || miss "$p (brew install $p failed)"
            done
        else
            for p in $need_pkgs; do miss "$p  (fix: brew install $p)"; done
        fi
    else
        for p in $need_pkgs; do
            [ "$p" = "python" ] && p="python3"
            you "$p is missing. Install it with your package manager, e.g.: sudo apt install $p"
        done
    fi
fi
have tmux && ok "tmux      $(command -v tmux)"
python_ok && ok "python3   $(command -v python3)"

# --- Antigravity CLI --------------------------------------------------------------------------
if ! have agy; then
    if [ "$INSTALL" = "1" ]; then
        echo "Installing the Antigravity CLI (official installer)..."
        curl -fsSL https://antigravity.google/cli/install.sh | bash || true
        hash -r 2>/dev/null
        have agy || miss "agy (installer did not produce ~/.local/bin/agy)"
    else
        miss "agy  (fix: curl -fsSL https://antigravity.google/cli/install.sh | bash)"
    fi
fi
have agy && ok "agy       $(command -v agy)  $(agy --version 2>/dev/null | head -n 1)"

# --- sign-in and models -----------------------------------------------------------------------
if have agy && python_ok; then
    if python3 "$HERE/orch.py" check >/tmp/manager-check.$$ 2>&1; then
        ok "signed in; Gemini 4 Hydrogen and Neon are available"
    else
        you "agy cannot see the Gemini 4 models. Run 'agy' once in Terminal and sign in with Google in the browser."
        info "Hydrogen and Neon need a Google AI Pro, Ultra or Enterprise plan."
        sed 's/^/          | /' /tmp/manager-check.$$ | tail -n 6
    fi
    rm -f /tmp/manager-check.$$
fi

echo
if [ "$need_user" = "1" ]; then
    echo "RESULT: waiting on you - do the [you] steps above, then run this again."
    exit 2
elif [ "$missing" = "1" ]; then
    echo "RESULT: not ready - run again with --install."
    exit 1
fi
echo "RESULT: ready."
