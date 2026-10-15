#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

# ============================================================
#  Benchmark Qwen3.8-27B on this machine.
#
#  The tuned values in the start scripts (-b 4096 -ub 1024,
#  q8_0 KV) are reasoned from the architecture, not measured.
#  This script measures them so you can confirm or override.
#
#  Sweeps, in order:
#    A. quant comparison  -- every Qwen3.8 weight file present
#    B. ubatch sweep      -- prefill throughput vs -ub
#    C. KV precision      -- f16 vs q8_0 vs q4_0
#    D. depth             -- throughput at realistic context depth
#
#  llama-bench does NOT exercise speculative decoding, so MTP
#  gains do not appear here. Measure that live -- see the note
#  at the end of the results file.
# ============================================================

BENCH="./llama-bin/llama-bench"
RESULTS="benchmark_results.txt"

if [ ! -x "$BENCH" ]; then
    echo "ERROR: $BENCH not found. Run the install script for your platform."
    exit 1
fi

echo "Stopping any running llama-server to free VRAM/unified memory..."
# kiln's own server, on the port the start scripts use.
. ./scripts/unix/lib_server.sh
kiln_stop_server 8080 || true
sleep 3

# ---- find the primary model, best first ----
MODEL=""
for f in \
    "models/Qwen3.8-27B-UD-Q5_K_XL.gguf" \
    "models/Qwen3.8-27B-UD-Q4_K_XL.gguf" \
    "models/Qwen3.8-27B-UD-Q6_K_XL.gguf" \
    "models/Qwen3.8-27B-Q8_0.gguf"
do
    if [ -z "$MODEL" ] && [ -f "$f" ]; then MODEL="$f"; fi
done

if [ -z "$MODEL" ]; then
    echo "ERROR: no Qwen3.8-27B weights found. Run: ./kiln.sh get"
    exit 1
fi

{
    echo "============================================================"
    echo " Qwen3.8-27B benchmark"
    date '+%Y-%m-%d %H:%M'
    echo " Host: $(uname -s) $(uname -m)"
    if command -v nvidia-smi >/dev/null 2>&1; then
        echo " GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
    fi
    "./llama-bin/llama-server" --version 2>&1 | grep -i build || true
    echo " Primary model: $MODEL"
    echo "============================================================"
    echo
} > "$RESULTS"

# ============================================================
echo "[A/4] Quant comparison..."
{
    echo "--- A. QUANT COMPARISON (pp512 / tg128, q8_0 KV) ---"
} >> "$RESULTS"
for f in \
    "models/Qwen3.8-27B-UD-IQ3_XXS.gguf" \
    "models/Qwen3.8-27B-UD-Q4_K_XL.gguf" \
    "models/Qwen3.8-27B-UD-Q5_K_XL.gguf" \
    "models/Qwen3.8-27B-UD-Q6_K_XL.gguf" \
    "models/Qwen3.8-27B-Q8_0.gguf"
do
    if [ -f "$f" ]; then
        echo "  - $(basename "$f")"
        "$BENCH" -m "$f" -ngl 99 -fa 1 -ctk q8_0 -ctv q8_0 \
            -p 512 -n 128 -r 3 >> "$RESULTS" 2>&1
    fi
done
echo >> "$RESULTS"

# ============================================================
echo "[B/4] Ubatch sweep (prefill throughput)..."
{
    echo "--- B. UBATCH SWEEP (-b 4096, varying -ub) ---"
    echo "  Larger -ub raises prefill speed but grows the compute buffer."
} >> "$RESULTS"
"$BENCH" -m "$MODEL" -ngl 99 -fa 1 -ctk q8_0 -ctv q8_0 \
    -b 4096 -ub 256,512,1024,2048 -p 4096 -n 0 -r 3 >> "$RESULTS" 2>&1
echo >> "$RESULTS"

# ============================================================
echo "[C/4] KV cache precision..."
{
    echo "--- C. KV PRECISION (speed cost of f16 vs q8_0 vs q4_0) ---"
    echo "  Quality is measured separately by scripts/unix/quality.sh."
} >> "$RESULTS"
for k in f16 q8_0 q4_0; do
    echo "  - KV $k"
    echo "  [KV=$k]" >> "$RESULTS"
    "$BENCH" -m "$MODEL" -ngl 99 -fa 1 -ctk "$k" -ctv "$k" \
        -p 512 -n 128 -r 3 >> "$RESULTS" 2>&1
done
echo >> "$RESULTS"

# ============================================================
echo "[D/4] Throughput at depth..."
{
    echo "--- D. THROUGHPUT AT CONTEXT DEPTH ---"
    echo "  Only 16 of 64 layers hold a KV cache, so decay with depth"
    echo "  should be far gentler than on a standard transformer."
} >> "$RESULTS"
"$BENCH" -m "$MODEL" -ngl 99 -fa 1 -ctk q8_0 -ctv q8_0 \
    -p 0 -n 128 -d 0,8192,32768,65536 -r 2 >> "$RESULTS" 2>&1
echo >> "$RESULTS"

cat >> "$RESULTS" <<'EOF'
============================================================
 READING THESE RESULTS
  pp = prefill tok/s   tg = generation tok/s

 Section B: if pp keeps climbing through ub=2048, raise
  --ubatch-size in the start script. If it plateaus or OOMs
  at 2048, keep 1024.

 Section C: q8_0 normally costs only a few percent vs f16
  while halving KV memory. If the gap is large, switch back
  to f16 and lower the context size instead.

 MTP speedup is NOT measured here -- llama-bench has no
  speculative-decoding path. Measure it live:
    1. start the server, note tg in server.log
    2. delete the MTP GGUF, restart, compare
============================================================
EOF

echo
echo "============================================================"
echo " Done. Results in $RESULTS"
echo "============================================================"
cat "$RESULTS"
