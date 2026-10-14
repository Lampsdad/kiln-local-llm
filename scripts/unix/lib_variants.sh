# shellcheck shell=bash
# Sourced by the download / start scripts. Shell twin of
# lib_variants.bat.
#
#   VARIANT=ablit ./start_linux.sh
#
# Sets: V_ID V_LABEL V_REPO V_PREFIX V_ALIAS V_UNCENSORED
#       V_THIRD_PARTY V_MTP V_VISION V_PARAMS V_QUANTS
#       V_MTP_FILES V_MMPROJ_FILES V_SPEC_N V_SAMPLING V_REASONING
#       V_MIN_BUILD
#
# The registry itself lives in scripts/hardware.py, which is also
# what sizes the launch -- one file knows what a variant IS, so
# adding a family does not mean editing four scripts and hoping they
# agree. This asks it and parses KEY=VALUE.
#
# The base/ablit fallback below exists for one case: Python missing
# on a machine that already has the weights (someone copied the GGUFs
# in by hand). It covers the two original 27B variants only -- every
# other family (9b, 4b, Gemma 4) is Python-path only, because you
# cannot have downloaded it without Python in the first place.

_kiln_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# KILN_PY, not `command -v python3`: on Windows that resolves to
# the Store alias stub, which runs, prints nothing and exits 0.
. "$(dirname "${BASH_SOURCE[0]}")/lib_python.sh"
_kiln_py="$KILN_PY"

# ── what does "no VARIANT given" mean? ──────────────────────
# It depends on who is asking, so the caller says:
#
#   KILN_PICK=hw    download.sh -- nothing is on disk yet, so the
#                   right default is whatever this GPU is sized for
#   KILN_PICK=disk  start_*.sh  -- weights exist, so run the best
#                   family actually present rather than insisting
#                   on base and failing
#   unset           plain "base", the historical behaviour
#
# An explicit VARIANT= always wins over both.
if [ -z "${VARIANT:-}" ]; then
    case "${KILN_PICK:-}" in
        hw)
            if [ -n "$_kiln_py" ]; then
                VARIANT="$("$_kiln_py" "$_kiln_root/scripts/hardware.py" \
                    --recommend 2>/dev/null | kiln_strip_cr |
                    sed -n 's/^REC_FAMILY=//p' | head -1)"
            fi
            ;;
        disk)
            # Best family first, so a machine holding several starts
            # the one you would have picked by hand. The order is
            # DISK_ORDER in hardware.py; the list below is the
            # no-Python fallback.
            if [ -n "$_kiln_py" ]; then
                VARIANT="$("$_kiln_py" "$_kiln_root/scripts/hardware.py" \
                    --pick-on-disk 2>/dev/null | kiln_strip_cr |
                    sed -n 's/^VARIANT=//p' | head -1)"
            else
                for _cand in "Qwen3.8-27B:base" \
                             "Huihui-Qwen3.8-27B-abliterated:ablit" \
                             "Qwen3.8-9B:9b" "Qwen3.8-4B:4b"; do
                    _pfx="${_cand%%:*}"
                    # A literal glob test: compgen is bash-only but
                    # this file is already bash (BASH_SOURCE above).
                    if compgen -G "$_kiln_root/models/${_pfx}-*.gguf" >/dev/null; then
                        VARIANT="${_cand##*:}"
                        break
                    fi
                done
            fi
            ;;
    esac
    : "${VARIANT:=base}"
fi

V_ID=""; V_LABEL=""; V_REPO=""; V_PREFIX=""; V_ALIAS=""
V_UNCENSORED=0; V_THIRD_PARTY=0; V_MTP=0; V_VISION=0
V_PARAMS=""; V_QUANTS=""
V_MTP_FILES=""; V_MMPROJ_FILES=""; V_SPEC_N=3; V_SAMPLING=""
V_REASONING=""; V_MIN_BUILD=0

if [ -n "$_kiln_py" ] && [ -f "$_kiln_root/scripts/hardware.py" ]; then
    # Read only the keys we expect. The output is ours, but parsing
    # by assignment rather than eval means a future field cannot
    # quietly execute anything here.
    while IFS='=' read -r _k _v; do
        case "$_k" in
            V_ID)          V_ID="$_v" ;;
            V_LABEL)       V_LABEL="$_v" ;;
            V_REPO)        V_REPO="$_v" ;;
            V_PREFIX)      V_PREFIX="$_v" ;;
            V_ALIAS)       V_ALIAS="$_v" ;;
            V_UNCENSORED)  V_UNCENSORED="$_v" ;;
            V_THIRD_PARTY) V_THIRD_PARTY="$_v" ;;
            V_MTP)         V_MTP="$_v" ;;
            V_VISION)      V_VISION="$_v" ;;
            V_PARAMS)      V_PARAMS="$_v" ;;
            V_QUANTS)      V_QUANTS="$_v" ;;
            V_MTP_FILES)   V_MTP_FILES="$_v" ;;
            V_MMPROJ_FILES) V_MMPROJ_FILES="$_v" ;;
            V_SPEC_N)      V_SPEC_N="$_v" ;;
            V_SAMPLING)    V_SAMPLING="$_v" ;;
            V_REASONING)   V_REASONING="$_v" ;;
            V_MIN_BUILD)   V_MIN_BUILD="$_v" ;;
        esac
    done < <("$_kiln_py" "$_kiln_root/scripts/hardware.py" \
                 --variant "${VARIANT:-base}" 2>/dev/null | kiln_strip_cr)
fi

if [ -z "$V_ID" ]; then
    # --- fallback: the two 27B variants, hardcoded ---------------
    # Why these two share their MTP head and vision projector:
    # huihui-ai's UD-* quants are re-ablations of the same
    # unsloth/Qwen3.8-27B-GGUF files, and the MTP and vision weights
    # are left unmodified by the ablation.
    case "${VARIANT:-base}" in
        base|stock)
            V_ID="base"
            V_LABEL="Qwen3.8-27B"
            V_REPO="unsloth/Qwen3.8-27B-GGUF"
            V_PREFIX="Qwen3.8-27B"
            V_ALIAS="qwen3.8-27b"
            V_UNCENSORED=0; V_MTP=1; V_VISION=1; V_PARAMS="27B"
            ;;
        ablit|abliterated)
            V_ID="ablit"
            V_LABEL="Qwen3.8-27B abliterated"
            V_REPO="huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF"
            V_PREFIX="Huihui-Qwen3.8-27B-abliterated"
            V_ALIAS="qwen3.8-27b-abliterated"
            V_UNCENSORED=1; V_MTP=1; V_VISION=1; V_PARAMS="27B"
            ;;
        *)
            echo "ERROR: unknown VARIANT '${VARIANT}'." >&2
            echo "       Use base, ablit, 9b, 4b, gemma-31b, gemma-26b," >&2
            echo "       gemma-12b or gemma-e4b." >&2
            echo "       (All but base and ablit need python3 on PATH.)" >&2
            exit 1
            ;;
    esac
    V_QUANTS="UD-Q8_K_XL Q8_0 UD-Q6_K_XL UD-Q5_K_XL UD-Q4_K_XL UD-IQ4_XS UD-Q3_K_XL UD-IQ3_S UD-IQ3_XXS UD-Q2_K_XL UD-IQ2_S"
    V_MTP_FILES="mtp-Qwen3.8-27B-Q8_0.gguf mtp-Qwen3.8-27B-Q4_0.gguf"
    V_MMPROJ_FILES="mmproj-F16.gguf mmproj-Qwen3.8-27B-Q8_0.gguf"
    V_SAMPLING="--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0"
fi

unset _kiln_root _kiln_py _k _v
