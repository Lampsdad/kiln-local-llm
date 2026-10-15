#!/usr/bin/env bash
# install_fedora.sh — llama.cpp + cloudflared on Fedora.
#
# NVIDIA: the Ubuntu CUDA build, plus the separate cudart tarball.
# Fedora does not ship libcudart, and that build looks for it beside
# the binary. The driver's reported CUDA version picks 12.8 or 13.4;
# a newer toolkit build will not load on an older driver. No NVIDIA
# card falls through to the Vulkan build, then the CPU build.
#
# /releases/latest is the empty v0.x line. The bNNNN nightlies are
# what actually contain llama-server. See lib_llama_bin.sh.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."

echo "============================================================"
echo " Local Model Runtime - Fedora Install"
echo "============================================================"
echo

if ! command -v nvidia-smi &>/dev/null; then
    echo "WARNING: nvidia-smi not found. Install the NVIDIA driver first."
    echo "         Continuing with the Vulkan build, or CPU if that is missing."
else
    echo "[OK] NVIDIA GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
fi

# ── llama-bin ────────────────────────────────────────────────
# shellcheck source=lib_llama_bin.sh
. "$SCRIPT_DIR/lib_llama_bin.sh"
kiln_install_llama_bin auto

# ── cloudflared ──────────────────────────────────────────────
if command -v cloudflared &>/dev/null; then
    echo "[OK] cloudflared already installed."
else
    echo "[*] Installing cloudflared..."
    CF_BIN="/usr/local/bin/cloudflared"
    if [ -w "$(dirname "$CF_BIN")" ]; then
        curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" \
            -o "$CF_BIN"
        chmod +x "$CF_BIN"
        echo "[OK] cloudflared installed to $CF_BIN"
    else
        echo "[*] Need sudo to install to /usr/local/bin..."
        sudo curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" \
            -o "$CF_BIN"
        sudo chmod +x "$CF_BIN"
        echo "[OK] cloudflared installed to $CF_BIN"
    fi
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
