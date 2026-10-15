#!/usr/bin/env bash
# ============================================================
#  kiln path -- put the kiln command on PATH.
#
#  Links ~/.local/bin/kiln to this checkout's kiln.sh. kiln.sh
#  follows the link back here, so `kiln` works from any directory.
#  No sudo. Run from another checkout, the link moves there.
#  The setup scripts run this too.
#
#    kiln_path.sh            add
#    kiln_path.sh --remove   undo
# ============================================================
set -euo pipefail

ROOT="$(cd -P "$(dirname "$0")/../.." && pwd -P)"
BIN="${HOME}/.local/bin"
LINK="${BIN}/kiln"

# A link to some kiln.sh is ours. Anything else called kiln is not
# ours to replace or delete.
ours() {
    [ -L "$LINK" ] && [ "$(basename "$(readlink "$LINK")")" = kiln.sh ]
}

case "${1:-}" in
    "") ;;
    --remove)
        if ours; then
            rm -f "$LINK"
            echo "[OK] Removed ~/.local/bin/kiln"
        elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
            echo "[WARN] ~/.local/bin/kiln was not made by kiln. Leaving it." >&2
            exit 1
        else
            echo "[OK] kiln was not on PATH. Nothing to remove."
        fi
        exit 0
        ;;
    *)
        echo "Usage: kiln path [--remove]" >&2
        exit 1
        ;;
esac

if { [ -e "$LINK" ] || [ -L "$LINK" ]; } && ! ours; then
    echo "[WARN] ~/.local/bin/kiln exists and was not made by kiln. Leaving it," >&2
    echo "       so kiln is not on PATH. Run ${ROOT}/kiln.sh instead." >&2
    exit 1
fi
mkdir -p "$BIN"
ln -sfn "${ROOT}/kiln.sh" "$LINK"
echo "[OK] kiln command: ~/.local/bin/kiln -> ${ROOT}/kiln.sh"

case ":${PATH}:" in
    *":${BIN}:"*|*":${BIN}/:"*) ;;
    *)
        # Most Linux profiles add ~/.local/bin at login once it exists.
        # macOS never does. The ~ here is printed, not expanded.
        # shellcheck disable=SC2088
        case "${SHELL##*/}" in
            zsh)  rc="~/.zshrc" ;;
            bash) if [ "$(uname -s)" = Darwin ]; then rc="~/.bash_profile"; else rc="~/.bashrc"; fi ;;
            *)    rc="~/.profile" ;;
        esac
        echo
        echo "  ~/.local/bin is not on your PATH yet. Add it, then open a new terminal:"
        echo "      echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ${rc}"
        ;;
esac
