#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."
export PATH="${HOME}/.local/bin:${PATH}"

# VARIANT=base (stock), VARIANT=ablit (abliterated), or any other
# family in scripts/hardware.py (9b, gemma-26b, ...).
# With no VARIANT set, resolve it from what is on disk -- see the
# KILN_PICK block in lib_variants.sh.
KILN_PICK=disk . "$SCRIPT_DIR/lib_variants.sh"

# Resolves MODEL / MTP / MMPROJ / FIT_ARGS against the weights
# actually on disk and the GPU actually present. QUANT= still
# overrides the choice of file.
echo "  build   : ${V_LABEL}"
. "$SCRIPT_DIR/lib_select.sh"
BINARY="./llama-bin/llama-server"
# 8080, as on every other platform: kiln opencode, kiln pi and the
# Cursor tunnel all assume it. slurm-llama.sh keeps 8081 for the
# cluster, with start-cloudflared-once.sh to match.
PORT=8080
LOG="./server.log"
CF_LOG="./cloudflared.log"
# Distinct per variant so Cursor cannot silently show the wrong one.
ALIAS="$V_ALIAS"

# ── pre-flight checks ────────────────────────────────────────
if [ ! -f "$BINARY" ]; then
    echo "ERROR: $BINARY not found. Run ./install_linux.sh first."
    exit 1
fi
if [ ! -f "$MODEL" ]; then
    echo "ERROR: $MODEL not found. Run: VARIANT=${V_ID} ./kiln.sh get"
    exit 1
fi

# ── API key (the tunnel URL is public — see lib_api_key.sh) ──
. "$SCRIPT_DIR/lib_api_key.sh"

# ── per-family flags: sampling, MTP head, vision (lib_select.sh) ─
kiln_model_args "$BINARY"

# ── stop any running instances ───────────────────────────────
echo "Stopping any existing llama-server (cloudflared left running for stable URL)..."
pkill -x llama-server 2>/dev/null || true
sleep 1

# ── start llama-server ───────────────────────────────────────
echo "Starting llama-server..."
# FIT_ARGS is either an explicit --ctx-size computed by
# scripts/hardware.py for the GPU present, or --fit on when there
# was nothing to compute from. Either way this script runs
# unmodified on machines with very different cards.
# q8_0 KV, not q4_0: 48 of Qwen3.8's 64 layers are recurrent, and
# quantization error accumulates along the sequence in those rather
# than being re-anchored each token.
"$BINARY" \
    --model        "$MODEL" \
    "${FIT_ARGS[@]}" \
    --n-gpu-layers 99 \
    --flash-attn   auto \
    --cache-type-k q8_0 \
    --cache-type-v q8_0 \
    --parallel     1 \
    --jinja \
    --reasoning-format deepseek \
    --port         $PORT \
    --host         0.0.0.0 \
    --alias        "$ALIAS" \
    --api-key-file "$API_KEY_FILE" \
    "${EXTRA[@]}" \
    --log-file     "$LOG" &
SERVER_PID=$!

# ── wait for server to be ready (health poll) ────────────────
echo "Waiting for server to load (1-2 min for a 30 GB model)..."
# The key goes to curl through a config on stdin rather than -H:
# argv is world-readable in /proc, and slurm-llama.sh runs this same
# poll on a shared cluster node. printf is a shell builtin, so the
# key never reaches another process's command line.
until printf 'header = "Authorization: Bearer %s"\n' "$API_KEY" \
        | curl -sf -K - "http://localhost:$PORT/health" >/dev/null 2>&1; do
    sleep 5
    # abort if server process died
    if ! kill -0 $SERVER_PID 2>/dev/null; then
        echo "ERROR: llama-server exited unexpectedly. Check $LOG for details."
        exit 1
    fi
done
echo "Server is ready."

# ── start cloudflared tunnel ─────────────────────────────────
# KILN_NO_TUNNEL=1 skips the tunnel: no public URL, though the LAN
# can still reach the port. The benchmark in kiln tui sets it, so
# measuring a setup never puts a server on the internet.
if [ -n "${KILN_NO_TUNNEL:-}" ] || ! command -v cloudflared &>/dev/null; then
    if [ -n "${KILN_NO_TUNNEL:-}" ]; then
        # This script leaves the tunnel up across restarts for a stable
        # URL, so one may still be forwarding to this port. No tunnel
        # means none.
        pkill -f "cloudflared tunnel --url http://127.0.0.1:${PORT}" 2>/dev/null || true
        echo "Tunnel skipped (KILN_NO_TUNNEL) -- no public URL."
    else
        echo "WARNING: cloudflared not found. Skipping tunnel."
    fi
    echo "The API is reachable at http://localhost:$PORT/v1"
    echo "API key: $API_KEY"
    echo "Press Ctrl+C to stop the server."
    wait $SERVER_PID
    exit 0
fi

echo "Starting Cloudflare tunnel (skip if already running — see start-cloudflared-once.sh)..."
if pgrep -f "cloudflared tunnel --url http://127.0.0.1:${PORT}" >/dev/null 2>&1; then
    echo "cloudflared already running; URL unchanged."
    if [ -f "./cloudflared.url" ]; then cat "./cloudflared.url"; fi
    CF_PID=""
else
# 127.0.0.1, not localhost: the pgrep above and
# start-cloudflared-once.sh both match on that literal string, so a
# mismatch here meant this script never recognised its own tunnel --
# every run started a second one and the URL changed anyway.
cloudflared tunnel --url "http://127.0.0.1:$PORT" >"$CF_LOG" 2>&1 &
CF_PID=$!
sleep 12
grep -o 'https://[^ ]*trycloudflare\.com' "$CF_LOG" | head -1 | tee cloudflared.url.tmp | xargs -I{} echo "{}/v1" | tee ./cloudflared.url
fi

echo
echo "============================================================"
echo "  API key (paste into Cursor's OpenAI API Key field):"
echo
echo "    $API_KEY"
echo
echo "  The tunnel URL is public. Requests without this key are"
echo "  rejected."
echo "============================================================"
echo
echo "Press Ctrl+C to stop everything."

# ── keep running until interrupted ───────────────────────────
trap 'echo; echo Shutting down...; kill $SERVER_PID 2>/dev/null; [ -n "${CF_PID:-}" ] && kill $CF_PID 2>/dev/null; exit 0' INT TERM
wait $SERVER_PID
