#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."

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
PORT=8080
LOG="./server.log"
CF_LOG="./cloudflared.log"

# ── pre-flight checks ────────────────────────────────────────
if [ ! -f "$BINARY" ]; then
    echo "ERROR: $BINARY not found. Run ./install_mac.sh first."
    exit 1
fi
if [ ! -f "$MODEL" ]; then
    echo "ERROR: $MODEL not found. Run: VARIANT=${V_ID} ./kiln.sh get"
    exit 1
fi

# ── API key (the tunnel URL is public — see lib_api_key.sh) ──
. "$SCRIPT_DIR/lib_api_key.sh"

# ── per-family flags: sampling, MTP head, vision (lib_select.sh) ─
# Before the stop below: a build too old for this family exits
# here, and should leave whatever is serving now alone.
kiln_model_args "$BINARY"

# ── stop any running instances ───────────────────────────────
echo "Stopping any existing instances..."
pkill -x llama-server 2>/dev/null || true
pkill -x cloudflared  2>/dev/null || true
sleep 1

# ── start llama-server ───────────────────────────────────────

# FIT_ARGS carries either a computed --ctx-size or --fit on.
# On Apple Silicon the computed number budgets ~75% of unified
# memory, which is roughly what Metal's
# recommendedMaxWorkingSetSize hands a single process -- --fit
# alone sees the whole pool and can be optimistic about it.
#
# ${A[@]+"${A[@]}"}, not "${A[@]}": macOS runs this with bash 3.2,
# where an empty array counts as unset under set -u and the launch
# dies. Before lib_select.sh added the sampling flags, EXTRA was
# empty for any model without an MTP head or projector.
echo "Starting llama-server (Metal)..."
"$BINARY" \
    --model        "$MODEL" \
    ${FIT_ARGS[@]+"${FIT_ARGS[@]}"} \
    --n-gpu-layers 99 \
    --flash-attn   auto \
    --cache-type-k q8_0 \
    --cache-type-v q8_0 \
    --parallel     1 \
    --jinja \
    --reasoning-format deepseek \
    --port         $PORT \
    --host         0.0.0.0 \
    --alias        "$V_ALIAS" \
    --api-key-file "$API_KEY_FILE" \
    ${EXTRA[@]+"${EXTRA[@]}"} \
    --log-file     "$LOG" &
SERVER_PID=$!

# ── wait for server to be ready (health poll) ────────────────
echo "Waiting for server to load (2-4 min for a 30 GB model on Apple Silicon)..."
# The key goes to curl through a config on stdin rather than -H:
# argv is world-readable in /proc, and slurm-llama.sh runs this same
# poll on a shared cluster node. printf is a shell builtin, so the
# key never reaches another process's command line.
until printf 'header = "Authorization: Bearer %s"\n' "$API_KEY" \
        | curl -sf -K - "http://localhost:$PORT/health" >/dev/null 2>&1; do
    sleep 5
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

echo "Starting Cloudflare tunnel..."
cloudflared tunnel --url "http://localhost:$PORT" >"$CF_LOG" 2>&1 &
CF_PID=$!
sleep 12

# ── print tunnel URL ─────────────────────────────────────────
echo
echo "============================================================"
grep -o 'https://[^ ]*trycloudflare\.com' "$CF_LOG" | head -1 | xargs -I{} echo " Base URL: {}/v1"
echo
echo " API key (paste into Cursor's OpenAI API Key field):"
echo "   $API_KEY"
echo
echo " The tunnel URL is public. Requests without the key are rejected."
echo "============================================================"
echo
echo "Press Ctrl+C to stop everything."

# ── keep running until interrupted ───────────────────────────
trap "echo; echo 'Shutting down...'; kill $SERVER_PID $CF_PID 2>/dev/null; exit 0" INT TERM
wait $SERVER_PID
