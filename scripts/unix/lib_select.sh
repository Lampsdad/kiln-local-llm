# shellcheck shell=bash
# ============================================================
#  Resolve what to load and how big to size it.
#
#  Sourced by start_linux.sh / start_mac.sh / start_fedora.sh
#  AFTER lib_variants.sh, which supplies V_PREFIX / V_QUANTS /
#  V_MTP_FILES / V_MMPROJ_FILES.
#
#  Sets:
#    MODEL      path to the weights that will be loaded
#    MTP        path to the MTP head, or empty
#    MMPROJ     path to the vision projector, or empty
#    FIT_ARGS   array: either (--ctx-size N) or (--fit on)
#    CTX        the context that was computed, or 0
#
#  and defines kiln_model_args, which the start scripts call once
#  they know their binary.
#
#  Why this exists: the three start scripts each carried their own
#  copy of this preamble, hardcoded to UD-Q5_K_XL and one MTP
#  filename. Three copies meant three places to forget when a quant
#  was added, and all three assumed the 32 GB card this repo was
#  tuned on.
#
#  On --fit vs an explicit --ctx-size: llama.cpp's --fit on sizes
#  the window against free VRAM at load time and does it well. It
#  is kept as the fallback, and it is still what runs when Python
#  or nvidia-smi is unavailable. But it cannot know that this repo
#  wants ONE slot at a large window rather than a merely safe one,
#  and on Apple Silicon it sees the whole unified pool rather than
#  what Metal will actually hand a single process. Where
#  hardware.py can compute a number, that number is used.
# ============================================================

_sel_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_sel_root="$(cd "$_sel_dir/../.." && pwd)"
# KILN_PY, not `command -v python3` -- see lib_python.sh for why.
. "$_sel_dir/lib_python.sh"
_sel_py="$KILN_PY"
_sel_hw="$_sel_root/scripts/hardware.py"

MODEL=""
MTP=""
MMPROJ=""
CTX=0
FIT_ARGS=()

# ── weights ─────────────────────────────────────────────────
# An explicit QUANT wins. Otherwise take the best one present, in
# registry order -- V_QUANTS is ordered best quality first, so the
# first hit is the one you would have chosen by hand.
if [ -n "${QUANT:-}" ]; then
    MODEL="./models/${V_PREFIX}-${QUANT}.gguf"
else
    for _q in ${V_QUANTS}; do
        if [ -f "./models/${V_PREFIX}-${_q}.gguf" ]; then
            MODEL="./models/${V_PREFIX}-${_q}.gguf"
            QUANT="$_q"
            break
        fi
    done
    # Nothing on disk: name the historical default so the
    # "not found" message below points somewhere real.
    if [ -z "$MODEL" ]; then
        case "$V_ID" in
            9b|4b) QUANT="Q5_K_M" ;;
            *)     QUANT="UD-Q5_K_XL" ;;
        esac
        MODEL="./models/${V_PREFIX}-${QUANT}.gguf"
    fi
fi

# ── MTP head and vision projector ───────────────────────────
# The family lists its own, best precision first; kiln get
# downloads whichever the card had room for. Families without
# them list nothing.
#
# These are plain ifs rather than `[ -f x ] && VAR=y`: under
# set -e a failing test makes the list non-zero and aborts the
# script, which is how a missing mmproj used to kill start_mac.sh
# before it printed anything.
for _f in ${V_MTP_FILES:-}; do
    if [ -z "$MTP" ] && [ -f "./models/$_f" ]; then
        MTP="./models/$_f"
    fi
done
# --no-mtp (kiln.sh sets KILN_NO_MTP=1) trades the draft head for
# context: cleared here, before sizing, so the VRAM it would have used
# becomes KV cache. Generation is roughly half as fast without it.
if [ "${KILN_NO_MTP:-0}" = "1" ]; then
    MTP=""
fi
for _f in ${V_MMPROJ_FILES:-}; do
    if [ -z "$MMPROJ" ] && [ -f "./models/$_f" ]; then
        MMPROJ="./models/$_f"
    fi
done

# ── context ─────────────────────────────────────────────────
REC_SAME=1
REC_FAMILY=""
REC_QUANT=""
REC_CTX=0
TIER_LABEL=""

if [ -n "$_sel_py" ] && [ -f "$_sel_hw" ] && [ -f "$MODEL" ]; then
    while IFS='=' read -r _k _v; do
        case "$_k" in
            CTX)        CTX="$_v" ;;
            TIER_LABEL) TIER_LABEL="$_v" ;;
            REC_SAME)   REC_SAME="$_v" ;;
            REC_FAMILY) REC_FAMILY="$_v" ;;
            REC_QUANT)  REC_QUANT="$_v" ;;
            REC_CTX)    REC_CTX="$_v" ;;
        esac
    done < <("$_sel_py" "$_sel_hw" --size-model "$MODEL" \
                 --mtp "$MTP" --mmproj "$MMPROJ" 2>/dev/null \
                 | kiln_strip_cr || true)
fi

if [ "${CTX:-0}" -gt 0 ] 2>/dev/null; then
    FIT_ARGS=(--ctx-size "$CTX")
else
    # No GPU information, or no Python. llama.cpp sizes it itself.
    CTX=0
    FIT_ARGS=(--fit on)
fi

# ── say what was chosen, and what would have been ───────────
echo "  weights : $MODEL"
if [ -n "$TIER_LABEL" ]; then
    echo "  hardware: $TIER_LABEL"
fi
if [ "$CTX" -gt 0 ]; then
    echo "  context : $CTX  ($((CTX / 1024))K)"
else
    echo "  context : sized by llama.cpp (--fit on)"
fi
if [ -n "$MTP" ]; then
    echo "  MTP     : $(basename "$MTP")"
else
    echo "  MTP     : none"
fi
if [ -n "$MMPROJ" ]; then
    echo "  vision  : $(basename "$MMPROJ")"
fi

# What is on disk always wins -- re-picking behind the user's back
# would be worse than knowingly running the wrong file. Just say so.
if [ "$REC_SAME" = "0" ] && [ -n "$REC_QUANT" ]; then
    echo
    echo "  [note] on this GPU kiln would pick ${REC_FAMILY} ${REC_QUANT}"
    echo "         at $((REC_CTX / 1024))K context:  ./kiln.sh get ${REC_FAMILY}"
fi
echo

# ── kiln_model_args BINARY ──────────────────────────────────
# Fills EXTRA with the flags that depend on the family: sampling,
# reasoning, the MTP head and the projector. Exits when the build
# is too old for the family at all.
#
# MTP speculative decoding merged into llama.cpp in b9180
# (2026-05-16). Older builds reject --spec-type draft-mtp, so
# probe for it rather than assuming.
kiln_model_args() {
    local bin="$1" build=0
    EXTRA=()
    if [ "${V_MIN_BUILD:-0}" -gt 0 ]; then
        if [ -n "$KILN_PY" ]; then
            build="$("$KILN_PY" "$_sel_hw" --build "$bin" 2>/dev/null |
                     kiln_strip_cr | sed -n 's/^BUILD=//p' | head -1)"
        fi
        if [ "${build:-0}" -gt 0 ] && [ "$build" -lt "$V_MIN_BUILD" ]; then
            # setup skips a llama-bin/ that is already there, so the
            # old build has to move aside first. Moved, not deleted:
            # a failed setup should not cost the build that still
            # runs Qwen.
            echo "ERROR: ${V_LABEL} needs llama.cpp b${V_MIN_BUILD} or newer;"
            echo "       this is b${build}. For the newest build:"
            echo "       mv llama-bin llama-bin-backup && ./kiln.sh setup"
            exit 1
        elif [ "${build:-0}" -eq 0 ]; then
            echo "[WARN] could not read the llama.cpp build. ${V_LABEL}"
            echo "       needs b${V_MIN_BUILD} or newer."
        fi
    fi

    # Word-split on purpose: the registry keeps these to plain flags.
    # shellcheck disable=SC2206
    EXTRA+=(${V_SAMPLING:---temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0})
    if [ -n "${V_REASONING:-}" ]; then
        EXTRA+=(--reasoning "$V_REASONING")
    fi
    # The help is read whole, not piped into grep -q: grep quits at the
    # first match, and llama-server's SIGPIPE for the rest of its help
    # then fails the pipeline under pipefail, which turns MTP off with
    # no word about why. A smaller pipe buffer makes that likely.
    local bin_help="" mtp_ok=0
    if [ -n "$MTP" ]; then
        bin_help="$("$bin" --help 2>&1 || true)"
        case "$bin_help" in *draft-mtp*) mtp_ok=1 ;; esac
    fi
    if [ "$mtp_ok" = 1 ]; then
        EXTRA+=(--spec-type draft-mtp
                --spec-draft-model "$MTP"
                --spec-draft-ngl 99
                --spec-draft-n-max "${V_SPEC_N:-3}")
        echo "MTP speculative decoding: enabled"
    elif [ -n "$MTP" ]; then
        echo "MTP speculative decoding: unavailable (need llama.cpp b9180+)"
    else
        echo "MTP speculative decoding: off (no MTP head for this build)"
    fi
    if [ -n "$MMPROJ" ]; then
        EXTRA+=(--mmproj "$MMPROJ")
        echo "Vision: enabled"
    fi
}

unset _sel_dir _sel_root _sel_py _q _k _v _f
