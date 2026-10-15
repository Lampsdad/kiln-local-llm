#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

# ============================================================
#  Measure the real quality cost of each Qwen3.8-27B quant on
#  YOUR copies of the files, instead of trusting general
#  rules-of-thumb about quantization.
#
#  Method: KL-divergence against a near-lossless reference.
#    1. Q8_0 generates reference logits over a fixed corpus.
#    2. Each smaller quant is scored against those logits.
#
#  KL-divergence beats raw perplexity here because it measures
#  how far the whole output distribution moved, which is what
#  actually breaks tool-call JSON -- perplexity can look fine
#  while the argmax token flips.
#
#  Rough reading of the mean KLD number:
#    < 0.001  indistinguishable
#    ~ 0.005  safe for agentic coding
#    ~ 0.02   occasional malformed tool calls on long chains
#    > 0.05   visibly degraded instruction following
#
#  Cost: ~20-45 min per quant. Q8_0 (29 GB) must be present.
# ============================================================

PPL="./llama-bin/llama-perplexity"
REF="models/Qwen3.8-27B-Q8_0.gguf"
CORPUS="models/wikitext-2-raw/wiki.test.raw"
KLDBASE="models/kld-base-qwen3.8.dat"
OUT="quant_quality.txt"
CHUNKS=100

if [ ! -x "$PPL" ]; then
    echo "ERROR: $PPL not found. Run the install script for your platform."
    exit 1
fi

if [ ! -f "$REF" ]; then
    cat <<EOF
============================================================
 Missing reference model: $REF

 KL-divergence needs a near-lossless baseline to compare
 against. Download Q8_0 (29 GB) with:

   QUANT=Q8_0 ./kiln.sh get

 then re-run this script.
============================================================
EOF
    exit 1
fi

echo "Stopping any running llama-server to free VRAM..."
# kiln's own server, on the port the start scripts use.
. ./scripts/unix/lib_server.sh
kiln_stop_server 8080 || true
sleep 3

# ---- corpus ----
if [ ! -f "$CORPUS" ]; then
    echo "Downloading wikitext-2 evaluation corpus..."
    mkdir -p models
    curl -fsSL -o /tmp/wikitext2.zip \
        "https://huggingface.co/datasets/ggml-org/ci/resolve/main/wikitext-2-raw-v1.zip"
    unzip -oq /tmp/wikitext2.zip -d models
    rm -f /tmp/wikitext2.zip
    if [ ! -f "$CORPUS" ]; then
        echo " ERROR: could not obtain the corpus. Place a plain-text file at:"
        echo "   $CORPUS"
        exit 1
    fi
fi

{
    echo "============================================================"
    echo " Qwen3.8-27B quantization quality (KL-divergence vs Q8_0)"
    date '+%Y-%m-%d %H:%M'
    echo " Corpus: wikitext-2 test, $CHUNKS chunks"
    echo "============================================================"
    echo
} > "$OUT"

# ---- reference logits ----
if [ -f "$KLDBASE" ]; then
    echo "[1] Reference logits already present, reusing $KLDBASE"
else
    echo "[1] Generating reference logits from Q8_0 (this is the slow part)..."
    if ! "$PPL" -m "$REF" -f "$CORPUS" --kl-divergence-base "$KLDBASE" \
            --chunks "$CHUNKS" -ngl 99 -fa 1 -c 4096; then
        echo " ERROR: reference pass failed."
        exit 1
    fi
fi

# ---- score each quant ----
STEP=2
for f in \
    "models/Qwen3.8-27B-UD-Q6_K_XL.gguf" \
    "models/Qwen3.8-27B-UD-Q5_K_XL.gguf" \
    "models/Qwen3.8-27B-UD-Q4_K_XL.gguf" \
    "models/Qwen3.8-27B-UD-IQ4_XS.gguf" \
    "models/Qwen3.8-27B-UD-Q3_K_XL.gguf" \
    "models/Qwen3.8-27B-UD-IQ3_XXS.gguf"
do
    [ -f "$f" ] || continue
    echo "[$STEP] Scoring $(basename "$f") ..."
    {
        echo
        echo "------------------------------------------------------------"
        echo " $(basename "$f")"
        echo "------------------------------------------------------------"
    } >> "$OUT"
    "$PPL" -m "$f" -f "$CORPUS" --kl-divergence-base "$KLDBASE" --kl-divergence \
        --chunks "$CHUNKS" -ngl 99 -fa 1 -c 4096 >> "$OUT" 2>&1
    STEP=$((STEP + 1))
done

cat >> "$OUT" <<'EOF'

============================================================
 HOW TO READ THIS

 Look for "Mean KLD" in each block. Compare quants against
 each other, not against absolute thresholds.

 Also useful: "Same top p" -- the share of tokens where the
 quant picks the SAME most-likely token as Q8_0. For agentic
 coding this predicts tool-call reliability better than KLD,
 because a flipped argmax is what emits a broken brace.

 Decision rule: take the smallest quant whose Mean KLD is
 within ~2x of the next size up. A large jump marks the
 point where that quant stops being worth the VRAM saved.
============================================================
EOF

echo
echo "============================================================"
echo " Done. Results in $OUT"
echo
echo " The reference logits ($KLDBASE) are large. Delete them"
echo " once you have decided on a quant."
echo "============================================================"
cat "$OUT"
