#!/usr/bin/env bash
#SBATCH --job-name=local-llm
#SBATCH --partition=debug
#SBATCH --gres=gpu:rtx5090:1
#SBATCH --cpus-per-task=6
#SBATCH --mem=48G
#SBATCH --time=7-00:00:00
#SBATCH --output=slurm-llama-%j.out
# Run the Qwen3.8-27B API under Slurm so the GPU shows as allocated in squeue/sinfo.
# Other jobs without --gres=gpu can still use remaining CPUs/RAM on the node.
# Do NOT start this while a manual llama-server is already running (GPU double-book).

set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"
cd "${HOME}/research/Local-LLM-setup-for-cursor"

PORT=8081
# VARIANT=base (stock), VARIANT=ablit, or any family in hardware.py.
KILN_PICK=disk . ./scripts/unix/lib_variants.sh

# Resolves MODEL / MTP / MMPROJ / FIT_ARGS against what is on
# disk and the GPU the scheduler actually gave us -- which on a
# shared cluster is not always the card the job asked for.
. ./scripts/unix/lib_select.sh
BINARY="./llama-bin/llama-server"
LOG="./server.log"
CF_LOG="./cloudflared.log"

pkill -x cloudflared 2>/dev/null || true

# This binds 0.0.0.0 on a shared cluster node, so the key is not
# optional here — anyone who can reach the node can reach the API.
. ./scripts/unix/lib_api_key.sh

# The job requests a single RTX 5090 (32 GB), but lib_select.sh
# sizes against whatever was allocated rather than trusting that.
# Per-family flags: sampling, MTP head, vision (lib_select.sh).
kiln_model_args "$BINARY"

exec "$BINARY" \
  --model "$MODEL" \
  --n-gpu-layers 99 \
  "${FIT_ARGS[@]}" \
  --flash-attn auto \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --parallel 1 \
  --batch-size 4096 \
  --ubatch-size 1024 \
  --threads 6 \
  --jinja \
  --reasoning-format deepseek \
  --port "$PORT" \
  --host 0.0.0.0 \
  --alias "$V_ALIAS" \
  --api-key-file "$API_KEY_FILE" \
  "${EXTRA[@]}" \
  --log-file "$LOG"