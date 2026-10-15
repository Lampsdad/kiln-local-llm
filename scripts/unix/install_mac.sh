#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."

ARCH=$(uname -m)   # arm64 = Apple Silicon, x86_64 = Intel
# A shell running under Rosetta says x86_64 on Apple Silicon too,
# which would pick the Intel build and lose Metal.
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
    ARCH=arm64
fi

echo "============================================================"
echo " Local Model Runtime - macOS Install (Metal)"
echo " Architecture: $ARCH"
echo "============================================================"
echo

# ── llama-bin ────────────────────────────────────────────────
# The tag lookup is shared with Linux and Fedora: /releases/latest
# no longer ships binaries, and the Mac builds are now .tar.gz.
# shellcheck source=lib_llama_bin.sh
. "$SCRIPT_DIR/lib_llama_bin.sh"
kiln_install_llama_bin macos "$ARCH"

# ── cloudflared ──────────────────────────────────────────────
if command -v cloudflared &>/dev/null; then
    echo "[OK] cloudflared already installed."
elif command -v brew &>/dev/null; then
    # homebrew-core has it. Cloudflare's own tap moved it there.
    echo "[*] Installing cloudflared via Homebrew..."
    brew install cloudflared
    echo "[OK] cloudflared installed."
else
    echo "[*] Installing cloudflared (direct download)..."
    CF_BIN="/usr/local/bin/cloudflared"
    if [ "$ARCH" = "arm64" ]; then
        CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"
    else
        CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
    fi
    # A new Apple Silicon Mac may not have /usr/local/bin yet.
    sudo mkdir -p /usr/local/bin
    curl -fsSL "$CF_URL" | sudo tar -xz -C /usr/local/bin cloudflared
    sudo chmod +x "$CF_BIN"
    echo "[OK] cloudflared installed to $CF_BIN"
fi

# ── models dir ───────────────────────────────────────────────
mkdir -p models
echo "[OK] models/ directory ready."

# ── kiln on PATH ─────────────────────────────────────────────
# A ~/.local/bin/kiln link, so `kiln` works from any directory.
# Not fatal: ./kiln.sh still works from here without it.
bash scripts/unix/kiln_path.sh || true

echo
echo "============================================================"
echo " Setup complete! Next steps:"
echo "   1. ./kiln.sh get     -- downloads the ~30 GB model"
echo "   2. ./kiln.sh start   -- launches server + tunnel"
echo "============================================================"
if [ "$ARCH" = "arm64" ]; then
    echo
    echo "NOTE: On Apple Silicon, models run in unified memory (shared RAM/GPU)."
    echo "      A 30 GB model requires a Mac with at least 36 GB of memory."
else
    echo
    echo "NOTE: llama.cpp's Intel macOS build has no Metal backend, so models"
    echo "      run on the CPU."
fi
