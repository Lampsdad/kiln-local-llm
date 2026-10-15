#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."

# ============================================================
#  Download model weights, sized to the machine present.
#
#  Up to three files make a full install:
#    weights  -- per variant, see lib_variants.sh
#    MTP head -- speculative decoding, where the family has one
#    mmproj   -- vision encoder, likewise
#
#  Which head and projector, and from where, is per family in
#  scripts/hardware.py. The two Qwen3.8-27B variants share theirs
#  and fetch them once: huihui-ai ablates the language layers of
#  unsloth's own UD quants and leaves those two untouched.
#
#  WHAT CHANGED: QUANT used to default to UD-Q5_K_XL regardless
#  of the card, which is right on 32 GB and a guaranteed OOM on
#  12 GB. It now defaults to whatever scripts/hardware.py picks
#  for the GPU actually present. An explicit QUANT= still wins.
#
#    QUANT=UD-Q4_K_XL ./kiln.sh get     # override the quant
#    VARIANT=ablit    ./kiln.sh get     # override the build
#    VARIANT=9b       ./kiln.sh get     # smaller card
#    VARIANT=gemma-26b ./kiln.sh get    # Gemma 4, opt-in
# ============================================================

# With no VARIANT set, resolve it from this GPU -- see the
# KILN_PICK block in lib_variants.sh.
KILN_PICK=hw . "$SCRIPT_DIR/lib_variants.sh"

. "$SCRIPT_DIR/lib_python.sh"
if [ -z "$KILN_PY" ]; then
    echo "ERROR: no working Python found. Install Python 3.8+ first."
    echo "       (On Windows, a bare \"python3\" on PATH may be the"
    echo "        Microsoft Store stub rather than a real install.)"
    exit 1
fi

HW="$SCRIPT_DIR/../hardware.py"

# ── what does this machine want? ─────────────────────────────
# Read into a plain associative-free set of shell vars; the keys
# are ours and fixed, so a small case filter beats eval.
REC_QUANT=""; REC_CTX=0; REC_MTP_FILE=""; REC_MTP_REPO=""; REC_MTP_LOCAL=""
REC_MMPROJ_FILE=""; REC_MMPROJ_REPO=""; REC_MMPROJ_LOCAL=""; REC_TOTAL_GB=""
REC_BELOW_MIN=0; REC_BELOW_QUALITY=0; VRAM_MIB=0; TIER_LABEL=""; GPU_NAME=""; PROBED=0

read_plan() {
    while IFS='=' read -r k v; do
        case "$k" in
            PROBED)          PROBED="$v" ;;
            VRAM_MIB)        VRAM_MIB="$v" ;;
            GPU_NAME)        GPU_NAME="$v" ;;
            TIER_LABEL)      TIER_LABEL="$v" ;;
            REC_QUANT)       REC_QUANT="$v" ;;
            REC_CTX)         REC_CTX="$v" ;;
            REC_MTP_FILE)    REC_MTP_FILE="$v" ;;
            REC_MTP_REPO)    REC_MTP_REPO="$v" ;;
            REC_MTP_LOCAL)   REC_MTP_LOCAL="$v" ;;
            REC_MMPROJ_FILE) REC_MMPROJ_FILE="$v" ;;
            REC_MMPROJ_REPO) REC_MMPROJ_REPO="$v" ;;
            REC_MMPROJ_LOCAL) REC_MMPROJ_LOCAL="$v" ;;
            REC_TOTAL_GB)    REC_TOTAL_GB="$v" ;;
            REC_BELOW_MIN)   REC_BELOW_MIN="$v" ;;
            REC_BELOW_QUALITY) REC_BELOW_QUALITY="$v" ;;
        esac
    done < <("$@" 2>/dev/null | kiln_strip_cr || true)
}

# An explicit QUANT pins the plan to that file; otherwise let the
# card choose. Either way the same call resolves which MTP head
# and which mmproj precision fit alongside it.
if [ -n "${QUANT:-}" ]; then
    read_plan "$KILN_PY" "$HW" --recommend --family "$V_ID" --quant "$QUANT"
else
    read_plan "$KILN_PY" "$HW" --recommend --family "$V_ID"
    QUANT="${REC_QUANT:-}"
fi

if [ -z "$QUANT" ]; then
    # No GPU information, or nothing in this family loads on it.
    # Fall back to the historical default rather than refusing --
    # the user may be downloading on one machine to run on another.
    # `[ a ] || [ b ] && x=y` would be a set -e landmine here: when
    # both tests fail the list exits non-zero and takes the script
    # with it. Spell it out.
    case "$V_ID" in
        9b|4b) QUANT="Q5_K_M" ;;
        *)     QUANT="UD-Q5_K_XL" ;;
    esac
    echo "NOTE: could not size this GPU; defaulting to ${QUANT}."
    echo "      Set QUANT=... if that is not what you want."
fi

FILE="${V_PREFIX}-${QUANT}.gguf"

echo "============================================================"
echo " Download: ${V_LABEL} (${QUANT})"
echo " Source: ${V_REPO}"
if [ "$PROBED" = "1" ]; then
    echo " Detected: ${GPU_NAME} (${VRAM_MIB} MiB) -- ${TIER_LABEL}"
    if [ "${REC_CTX:-0}" -gt 0 ]; then
        echo " Context : $((REC_CTX / 1024))K tokens on this GPU"
    fi
fi
if [ "${V_THIRD_PARTY:-0}" = "1" ]; then
    echo
    echo " NOTE: this is a third-party distillation, not a Qwen"
    echo "       release, and has no MTP head or vision tower."
fi
if [ "$REC_BELOW_MIN" = "1" ]; then
    echo
    echo " [WARN] this leaves less context than agentic coding really"
    echo "        wants. A smaller quant, or a smaller model, would"
    echo "        give you a much larger window."
fi
if [ "$REC_BELOW_QUALITY" = "1" ]; then
    echo
    echo " NOTE: a low-bit quant, below the quality floor; expect weaker output."
fi
echo "============================================================"
echo

# Fedora and Debian refuse a plain pip install into the system
# interpreter (PEP 668). Use huggingface_hub if it is already
# importable. Otherwise try a normal install, then a user install
# that is allowed to touch an externally managed environment.
if "$KILN_PY" -c 'import huggingface_hub' >/dev/null 2>&1; then
    echo "[OK] huggingface_hub already available."
else
    echo "[*] Installing huggingface_hub..."
    if ! "$KILN_PY" -m pip install -q "huggingface_hub>=0.22"; then
        echo "[*] System pip refused the install. Retrying for this user only."
        "$KILN_PY" -m pip install -q --user --break-system-packages "huggingface_hub>=0.22"
    fi
fi
# Optional. A missing module makes huggingface_hub abort the whole
# download when this variable is set, so only turn it on when the
# import works. Failure to install it is not fatal.
if ! "$KILN_PY" -c 'import hf_transfer' >/dev/null 2>&1; then
    "$KILN_PY" -m pip install -q --user --break-system-packages hf_transfer >/dev/null 2>&1 || true
fi
if "$KILN_PY" -c 'import hf_transfer' >/dev/null 2>&1; then
    export HF_HUB_ENABLE_HF_TRANSFER=1
else
    echo "[*] hf_transfer is not installed. The download still works, just slower."
fi

mkdir -p models

# fetch REPO NAME [LOCAL]: NAME from REPO into models/LOCAL, which
# defaults to NAME. A different LOCAL is how two publishers' files of
# the same name (unsloth's mmproj-F16.gguf, for Qwen and for Gemma)
# both fit in models/: it downloads aside, then moves into place.
fetch() {
    local repo="$1" name="$2" local_name="${3:-$2}"
    if [ -f "models/$local_name" ]; then
        echo "[OK] already present: $local_name"
        return 0
    fi
    echo "[*] Downloading $name from $repo ..."
    "$KILN_PY" - "$repo" "$name" "$local_name" <<'EOF'
import os, sys
from huggingface_hub import hf_hub_download
repo, name, local = sys.argv[1], sys.argv[2], sys.argv[3]
if name == local:
    path = hf_hub_download(repo_id=repo, filename=name, local_dir="models")
else:
    got = hf_hub_download(repo_id=repo, filename=name,
                          local_dir=os.path.join("models", ".download"))
    path = os.path.join("models", local)
    os.replace(got, path)
print(f"    saved: {path} ({os.path.getsize(path)/1e9:.1f} GB)")
EOF
}

fetch "$V_REPO" "$FILE"
# Only for families that have them, and only when the card left
# room for them -- hardware.py returns empty names otherwise.
# Plain ifs, not `[ -n x ] && fetch`: under set -e a test that
# fails makes the whole list non-zero and aborts the script, so the
# no-MTP case would exit silently right before the summary.
if [ -n "$REC_MMPROJ_FILE" ]; then
    fetch "$REC_MMPROJ_REPO" "$REC_MMPROJ_FILE" "$REC_MMPROJ_LOCAL"
fi
if [ -n "$REC_MTP_FILE" ]; then
    fetch "$REC_MTP_REPO" "$REC_MTP_FILE" "$REC_MTP_LOCAL"
fi

echo
echo "Done. Launch with VARIANT=${V_ID} ./kiln.sh start"
