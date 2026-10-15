#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

ARCH=$(uname -m)   # arm64 = Apple Silicon, x86_64 = Intel

echo "============================================================"
echo " Local Model Runtime - macOS Install (Metal)"
echo " Architecture: $ARCH"
echo "============================================================"
echo

# ── llama-bin ────────────────────────────────────────────────
if [ -f "llama-bin/llama-server" ]; then
    echo "[OK] llama-bin/ already populated, skipping download."
else
    echo "[*] Fetching latest llama.cpp release from GitHub..."
    mkdir -p llama-bin

    if [ "$ARCH" = "arm64" ]; then
        ARCH_PAT="macos.*arm64|darwin.*arm64"
    else
        ARCH_PAT="macos.*x64|darwin.*x64|macos.*x86_64"
    fi

    ASSET_URL=$(curl -fsSL https://api.github.com/repos/ggml-org/llama.cpp/releases/latest \
        | python3 -c "
import sys, json, re
rel = json.load(sys.stdin)
pat = r'$ARCH_PAT'
for a in rel['assets']:
    if re.search(pat, a['name'], re.IGNORECASE) and a['name'].endswith('.zip'):
        print(a['browser_download_url']); sys.exit(0)
print('NOT_FOUND'); sys.exit(1)
")

    if [ "$ASSET_URL" = "NOT_FOUND" ]; then
        echo "ERROR: Could not find a macOS $ARCH asset in the latest release."
        echo "Download manually from: https://github.com/ggml-org/llama.cpp/releases/latest"
        echo "Extract into llama-bin/"
        exit 1
    fi

    FILENAME=$(basename "$ASSET_URL")
    echo "Downloading $FILENAME..."
    curl -fL "$ASSET_URL" -o "llama-bin/$FILENAME"
    unzip -q "llama-bin/$FILENAME" -d llama-bin
    rm "llama-bin/$FILENAME"
    chmod +x llama-bin/llama-server llama-bin/llama-cli 2>/dev/null || true
    echo "[OK] llama.cpp binaries extracted."
fi

# ── cloudflared ──────────────────────────────────────────────
if command -v cloudflared &>/dev/null; then
    echo "[OK] cloudflared already installed."
elif command -v brew &>/dev/null; then
    echo "[*] Installing cloudflared via Homebrew..."
    brew install cloudflare/cloudflare/cloudflared
    echo "[OK] cloudflared installed."
else
    echo "[*] Installing cloudflared (direct download)..."
    CF_BIN="/usr/local/bin/cloudflared"
    if [ "$ARCH" = "arm64" ]; then
        CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"
    else
        CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
    fi
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
fi
