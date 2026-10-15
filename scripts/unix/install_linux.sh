#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."

echo "============================================================"
echo " Local Model Runtime - Linux Install"
echo "============================================================"
echo

# ── CUDA check ───────────────────────────────────────────────
if ! command -v nvidia-smi &>/dev/null; then
    echo "WARNING: nvidia-smi not found, so this installs the Vulkan build, which"
    echo "         can still drive a GPU outside WSL. Without one it falls through"
    echo "         to the CPU build."
else
    echo "[OK] NVIDIA GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
fi

# ── llama-bin ────────────────────────────────────────────────
# NVIDIA gets the CUDA build matched to the driver, with its cudart
# tarball; anything else gets Vulkan, then CPU. That holds under WSL
# too: WSL passes CUDA through (libcuda is in /usr/lib/wsl/lib), while
# its Vulkan sees no GPU at all, so the Vulkan build ran on the CPU.
# On a 5090 the WSL CUDA build matched the Windows one: 63 vs 65
# tok/s generating, 3,680 vs 3,646 tok/s on a 4K prompt (b11669).
# The tag lookup is shared with Fedora: /releases/latest no longer
# ships binaries.
# shellcheck source=lib_llama_bin.sh
. "$SCRIPT_DIR/lib_llama_bin.sh"
kiln_install_llama_bin auto

# ── cloudflared ──────────────────────────────────────────────
if command -v cloudflared &>/dev/null; then
    echo "[OK] cloudflared already installed."
else
    echo "[*] Installing cloudflared..."
    CF_BIN="${HOME}/.local/bin/cloudflared"
    mkdir -p "$(dirname "$CF_BIN")"
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" \
        -o "$CF_BIN"
    chmod +x "$CF_BIN"
    echo "[OK] cloudflared installed to $CF_BIN (ensure ~/.local/bin is on PATH)"
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
