#!/usr/bin/env python3
"""
Size a launch to the machine actually present.

Everything in this repo was tuned on an RTX 5090 (32 GB) + Ryzen 9
9900X3D. Hardcoding those numbers means anyone on a 12/16/24 GB card
OOMs on the first run -- or, worse, silently downloads 21 GB of weights
that will never load. So derive the choice instead.

WHY THIS IS PYTHON AND NOT A .bat/.sh TWIN
------------------------------------------
The rest of the repo twins its libraries (lib_variants.bat /
lib_variants.sh) because they hold a dozen lines of data each. This
holds a model registry, a KV-cache model, and a search over both. Two
copies of that drift the first time a quant is added, and the failure
mode of drift here is an OOM on someone else's machine. Python is
already a hard dependency -- download.bat and download.sh both refuse
to run without it -- so this costs no new install.

Callers get KEY=VALUE lines and parse them the same way on both
platforms. If Python is somehow missing, callers fall back to their
previous behaviour (Windows: the arithmetic in probe_hardware.ps1;
Unix: llama.cpp's own --fit on).

THE ARITHMETIC
--------------
Context sizing is arithmetic, not a lookup table. Qwen3.5-architecture
models interleave three GatedDeltaNet layers for every full-attention
layer (`full_attention_interval: 4`), and ONLY the full-attention
layers hold a KV cache -- the linear layers carry a small fixed
recurrent state instead. That is why the context windows here look
implausibly large next to a dense transformer of the same size.

    KV bytes/token = full_attn_layers x kv_heads x head_dim x 2 (K+V)
                     x 34/32   (q8_0: 34 bytes per 32-element block)

    27B: 16 x 4 x 256 x 2 = 32,768 elem -> 34,816 B/token
    9B : 8 x 4 x 256 x 2 = 16,384 elem -> 17,408 B/token
    4B : same attention shape as 9B     -> 17,408 B/token

All three numbers are read off the published config.json, not guessed:
num_hidden_layers / full_attention_interval gives the full-attention
count, and num_key_value_heads x head_dim gives the per-layer width.

Gemma 4 gets the same treatment with sliding-window layers in place of
the linear ones: only its global layers grow with context, and the
sliding-window cache is a fixed cost like the recurrent state. The
numbers are worked through above its registry entries.

Usage:
    hardware.py --detect                 what is in this machine
    hardware.py --recommend              what to download and run on it
    hardware.py --size-model PATH [...]  context for a file already on disk
    hardware.py --table [gemma]          the README matrix
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections import namedtuple

MIB = 1024 * 1024

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(os.path.dirname(HERE), "models")

# ---------------------------------------------------------------
#  Fixed VRAM costs that do not scale with context length.
#  Measured on the 5090; they are the same allocations on any card.
# ---------------------------------------------------------------
COMPUTE_BUF_MIB = 2048   # at --ubatch-size 1024
DRIVER_RESERVE  = 1536   # desktop compositor / driver overhead

# Allocation is not exact: buffers fragment, the compute buffer grows
# with batch shape, and the driver's own footprint moves around. Aiming
# at 100% of the arithmetic budget reliably OOMs.
SAFETY = 0.85

# The context this repo is actually for: agentic coding in Cursor,
# which sends tens of thousands of tokens before the model writes a
# line. Quality is maximised SUBJECT TO clearing this bar, so the bar
# is what decides every trade below -- set it wrong and the search
# happily returns a better quant with a useless window.
#
# 96K is not arbitrary. It is the bar that reproduces the choice this
# repo hand-validated on a 32 GB card: UD-Q5_K_XL with the q8_0 MTP
# head and vision, at 112K. Lower it to 64K and UD-Q6_K_XL wins with
# the small MTP head, no vision and 64K -- a better model that is
# worse at the job. Raise it to 128K and nothing fits 32 GB at all.
#
# Override for a different job: KILN_MIN_CTX=32768 favours quality on
# short prompts, KILN_MIN_CTX=200000 favours window over everything.
MIN_CTX = int(os.environ.get("KILN_MIN_CTX", 98304))

# Round context down to whole blocks; llama.cpp pads to these anyway.
CTX_BLOCK = 4096


# ---------------------------------------------------------------
#  Model registry
#
#  Sizes are exact byte counts from the Hugging Face file listings,
#  read 2026-09-04. They drive the download menu, the free-space
#  check and every context number below, so they are recorded rather
#  than rounded.
#
#  Quant ladders are ordered best quality first. Every 27B name below
#  exists in BOTH the unsloth and the huihui-ai repo -- huihui
#  re-ablates unsloth's own UD quants, so the ladders line up. Names
#  present in only one repo (unsloth's UD-IQ1_*, huihui's UD-DW-*)
#  are deliberately left out so one ladder covers both variants.
# ---------------------------------------------------------------

# A file that rides along with the weights: an MTP draft head or a
# vision projector. `remote` is its name in the Hugging Face repo and
# `local` its name in models/. They differ where two publishers use
# one name for different files: unsloth calls both the Qwen and the
# Gemma projector mmproj-F16.gguf, and a download that finds a file of
# that name already present skips it.
Extra = namedtuple("Extra", "repo remote local bytes")


def _extra(repo, remote, nbytes, local=None):
    return Extra(repo, remote, local or remote, nbytes)


# MTP heads and the vision projector are 27B-only, and are shared by
# the stock and abliterated variants: huihui ablates the language
# layers and leaves these untouched.
QWEN_MTP_REPO = "ggml-org/Qwen3.8-27B-GGUF"
QWEN_MTP_HEADS = {
    # best first
    "q8_0": _extra(QWEN_MTP_REPO, "mtp-Qwen3.8-27B-Q8_0.gguf", 3164006688),
    "q4_0": _extra(QWEN_MTP_REPO, "mtp-Qwen3.8-27B-Q4_0.gguf", 1680271648),
}

QWEN_MMPROJ = {
    "f16":  _extra("unsloth/Qwen3.8-27B-GGUF", "mmproj-F16.gguf", 927607488),
    "q8_0": _extra(QWEN_MTP_REPO, "mmproj-Qwen3.8-27B-Q8_0.gguf", 629247008),
}

# Qwen's published thinking-mode values.
QWEN_SAMPLING = "--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0"

# Lowest quant index still worth serving, per family. Below this the
# model stops being good at the job rather than merely getting worse
# at it: the README's own quality sweep puts UD-IQ3_XXS as
# "noticeably weaker on agentic tool-calling", and tool-calling is
# most of what Cursor asks for. When a card cannot reach this floor,
# the right answer is a smaller model at a decent quant, not a 27B
# crushed into 2 bits -- which is what the distill families are for.

QUANTS_27B = [
    # (name, unsloth bytes, huihui bytes)
    ("UD-Q8_K_XL", 31457991680, 31457991680),
    ("Q8_0",       29047086048, 29047084320),
    ("UD-Q6_K_XL", 25299061664, 24826588064),
    ("UD-Q5_K_XL", 20876938144, 20673612704),
    ("UD-Q4_K_XL", 17559178144, 17378626464),
    ("UD-IQ4_XS",  14252845984, 14403496864),
    ("UD-Q3_K_XL", 13146393504, 13326125984),
    ("UD-IQ3_S",   12040883104, 11952901024),
    ("UD-IQ3_XXS", 10934860704, 11021122464),
    ("UD-Q2_K_XL",  9828981664, 10011909024),
    ("UD-IQ2_S",    8371970048,  8424398848),
]

# What every Qwen3.8 family shares; each entry below adds its own.
#
#   state_mib     VRAM that does not grow with context: here the
#                 GatedDeltaNet layers' fixed recurrent state
#   draft_mib     what the MTP head takes beyond its file: here its
#                 own small KV cache
#   draft_ctx_bytes  and per token of context, on top of kv_bytes
#   reasoning     the --reasoning value, or "" for the template's
#                 default, which for Qwen is to think
#   min_build     llama.cpp build below which start refuses. 0: Qwen
#                 runs on any build, and the MTP head is probed for
#                 separately (it needs b9180)
_QWEN = {
    "group": "qwen",
    "uncensored": False,
    "draft_mib": 512,
    "draft_ctx_bytes": 0,
    "spec_n_max": 3,        # llama.cpp's default; matches the released head
    "sampling": QWEN_SAMPLING,
    "reasoning": "",
    "min_build": 0,
    "mtp_heads": {},
    "mmproj": {},
}

MODELS = {
    "base": dict(_QWEN, **{
        "label": "Qwen3.8-27B",
        "repo": "unsloth/Qwen3.8-27B-GGUF",
        "prefix": "Qwen3.8-27B",
        "alias": "qwen3.8-27b",
        "params": "27B",
        "kv_bytes": 34816,      # 16 full-attn layers
        "state_mib": 160,       # 48 GatedDeltaNet layers, fixed state
        "max_ctx": 262144,
        "mtp_heads": QWEN_MTP_HEADS,
        "mmproj": QWEN_MMPROJ,
        "quants": [(n, a) for n, a, _ in QUANTS_27B],
        "min_quality": 5,       # UD-IQ4_XS; see MIN_QUALITY note above
        "rank": 3,              # quality rank across families, high = better
    }),
    "ablit": dict(_QWEN, **{
        "label": "Qwen3.8-27B abliterated",
        "repo": "huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF",
        "prefix": "Huihui-Qwen3.8-27B-abliterated",
        "alias": "qwen3.8-27b-abliterated",
        "uncensored": True,
        "params": "27B",
        "kv_bytes": 34816,
        "state_mib": 160,
        "max_ctx": 262144,
        "mtp_heads": QWEN_MTP_HEADS,
        "mmproj": QWEN_MMPROJ,
        "quants": [(n, b) for n, _, b in QUANTS_27B],
        "min_quality": 5,
        "rank": 3,
    }),
    # --- smaller families, for cards a 27B has no business on -------
    #
    # Qwen shipped no small dense sibling in the 3.8 family: the
    # official lineup is 27B, Flash-Next (180B MoE) and 2.4T-A95B.
    # These two are third-party distillations of Qwen3.8-2.4T-A95B
    # into the Qwen3.5-9B / -4B architectures by Empero, Apache-2.0.
    #
    # They are NOT the same model as the 27B and are marked
    # third_party so every surface that offers them says so. Both are
    # text-only: the GGUF repos ship no mmproj and no MTP head, so
    # vision and the speculative-decoding speedup are unavailable
    # regardless of card.
    "9b": dict(_QWEN, **{
        "label": "Qwen3.8-9B distill",
        "repo": "empero-ai/Qwen3.8-9B-Distill-GGUF",
        "prefix": "Qwen3.8-9B",
        "alias": "qwen3.8-9b",
        "params": "9B",
        "kv_bytes": 17408,      # 8 full-attn layers
        "state_mib": 64,        # 24 linear layers, narrower state
        "max_ctx": 262144,
        "third_party": True,
        "quants": [
            ("Q8_0",   9786060096),
            ("Q6_K",   7558901056),
            ("Q5_K_M", 6642543936),
            ("Q4_K_M", 5780090176),
        ],
        "min_quality": 3,       # Q4_K_M; the whole ladder is usable
        "rank": 2,
    }),
    "4b": dict(_QWEN, **{
        "label": "Qwen3.8-4B distill",
        "repo": "empero-ai/Qwen3.8-4B-Distill-GGUF",
        "prefix": "Qwen3.8-4B",
        "alias": "qwen3.8-4b",
        "params": "4B",
        "kv_bytes": 17408,
        "state_mib": 64,
        "max_ctx": 262144,
        "third_party": True,
        "quants": [
            ("Q8_0",   4610579744),
            ("Q6_K",   3563027744),
            ("Q5_K_M", 3161425184),
            ("Q4_K_M", 2783446304),
        ],
        "min_quality": 3,
        "rank": 1,
    }),
}

# --- Gemma 4: opt-in ---------------------------------------------
#
# Google's open family, Apache-2.0, from unsloth's GGUF repos, which
# ship the UD quants, the vision projector and an MTP draft head side
# by side. kiln never picks these for you (they are in no
# FAMILY_ORDER); they run when named: kiln start gemma-26b.
#
# The architecture interleaves five sliding-window layers with one
# global layer. llama.cpp keeps the sliding layers' cache at window +
# ubatch cells whatever the context, so -- as with Qwen's DeltaNet
# state -- only the global layers grow with context. Per token:
#
#   KV       global layers x global KV heads x 512 head-dim x 2 (K+V)
#            x 34/32 (q8_0). Gemma's global layers share one projection
#            for K and V, but llama.cpp normalises and caches them
#            separately, so the 2 stays.
#   scratch  one global layer's K+V again at f16 (flash attention
#            dequantises it), plus the f16 mask: 2 bytes x 1024
#            ubatch. Qwen pays this too, but against its larger KV it
#            disappears into SAFETY; against Gemma's small one it is a
#            fifth to two-fifths of the bill, so it is counted.
#   kv_bytes = KV + scratch.  The MTP head attends over the same
#            context and pays the scratch again: draft_ctx_bytes.
#
#   state_mib = sliding layers x KV heads x 256 x 2 x
#               (window + 1024 ubatch) x 34/32
#
#            global x heads   KV     scratch  kv_bytes  sliding   state
#   31B      10 x 4         43,520   10,240    53,760   50 x 16   850 MiB
#   26B-A4B   5 x 2         10,880    6,144    17,024   25 x 8    213 MiB
#   12B       8 x 1          8,704    4,096    12,800   40 x 8    340 MiB
#   E4B       4 x 2          8,704    6,144    14,848   20 x 2     32 MiB
#            (E4B: 512 window; of 42 layers the last 18 reuse earlier
#            layers' cache)
#
# Read off each config.json (layer_types, num_global_key_value_heads,
# global_head_dim, sliding_window, num_kv_shared_layers), then checked
# on the 5090 at b11624 by loading each model at two contexts: per
# token the 31B took 53,792 B, the 12B 12,792-12,960 and the E4B
# 14,965, with the MTP head adding 4,059 per token on the 12B. The
# fixed costs came in under COMPUTE_BUF_MIB + state_mib by ~1.4 GB,
# which is margin, not error. The 26B was not loaded.
#
# The MTP heads are Google's "assistant" drafters (llama.cpp arch
# gemma4-assistant). They read the target's KV cache rather than
# keeping one: their fixed cost measured 4-163 MiB beyond the file.
#
# E4B's per-layer embedding table (256 x 42 layers x the 262K vocab)
# stays in system RAM, so that much of each file never reaches the
# GPU: host_bytes, per quant, from the GGUF headers.
#
# min_build: Gemma MTP landed in b9549, but the drafter (b10742),
# vision (b10798) and required-tool-call grammar (b11059) fixes came
# later. Tool calls are most of what Cursor sends.
GEMMA_SAMPLING = "--temp 1.0 --top-p 0.95 --top-k 64 --min-p 0.0"


def _gemma_extras(repo, stem, mtp_bytes, mmproj_bytes):
    """The MTP head at the repo root (the Q8_0 one) and the F16
    projector, renamed so it cannot collide with Qwen's."""
    return {
        "mtp_heads": {"q8_0": _extra(repo, "mtp-%s.gguf" % stem, mtp_bytes)},
        "mmproj": {"f16": _extra(repo, "mmproj-F16.gguf", mmproj_bytes,
                                 local="mmproj-%s-F16.gguf" % stem)},
    }


_GEMMA = {
    "group": "gemma",
    "uncensored": False,
    "draft_mib": 256,
    "spec_n_max": 4,        # unsloth's measured setting
    "sampling": GEMMA_SAMPLING,
    # Gemma 4 thinks only when asked; Qwen's template thinks by default.
    "reasoning": "on",
    "min_build": 11059,
    "min_quality": 5,       # IQ4_XS-class, as for the Qwen 27B
}

MODELS.update({
    "gemma-31b": dict(_GEMMA, **dict({
        "label": "Gemma 4 31B",
        "repo": "unsloth/gemma-4-31B-it-GGUF",
        "prefix": "gemma-4-31B-it",
        "alias": "gemma-4-31b",
        "params": "31B",
        "kv_bytes": 53760,
        "draft_ctx_bytes": 10240,
        "state_mib": 850,
        "max_ctx": 262144,
        "quants": [
            ("UD-Q8_K_XL", 35020041152),
            ("Q8_0",       32635677632),
            ("UD-Q6_K_XL", 27521338304),
            ("UD-Q5_K_XL", 21890429888),
            ("UD-Q4_K_XL", 18822970304),
            ("IQ4_XS",     16372460480),
            ("UD-Q3_K_XL", 15380222912),
            ("UD-IQ3_XXS", 11837782976),
            ("UD-Q2_K_XL", 11774991296),
            ("UD-IQ2_M",   10752820160),
        ],
    }, **_gemma_extras("unsloth/gemma-4-31B-it-GGUF", "gemma-4-31B-it",
                       514687104, 1198957024))),
    "gemma-26b": dict(_GEMMA, **dict({
        "label": "Gemma 4 26B-A4B",
        "repo": "unsloth/gemma-4-26B-A4B-it-GGUF",
        "prefix": "gemma-4-26B-A4B-it",
        "alias": "gemma-4-26b-a4b",
        "params": "26B-A4B",
        "kv_bytes": 17024,
        "draft_ctx_bytes": 6144,
        "state_mib": 213,
        "max_ctx": 262144,
        "quants": [
            ("UD-Q8_K_XL", 27636232928),
            ("Q8_0",       26859861728),
            ("UD-Q6_K_XL", 23295391456),
            ("UD-Q5_K_XL", 21217769184),
            ("UD-Q4_K_XL", 17010980576),
            ("UD-IQ4_XS",  13597177568),
            ("UD-Q3_K_XL", 12907280096),
            ("UD-IQ3_XXS", 11416548832),
            ("UD-Q2_K_XL", 10546934240),
            ("UD-IQ2_M",   10014755296),
        ],
    }, **_gemma_extras("unsloth/gemma-4-26B-A4B-it-GGUF",
                       "gemma-4-26B-A4B-it", 461766816, 1193058784))),
    # unsloth spells this one in lower case, repo and files alike.
    "gemma-12b": dict(_GEMMA, **dict({
        "label": "Gemma 4 12B",
        "repo": "unsloth/gemma-4-12b-it-GGUF",
        "prefix": "gemma-4-12b-it",
        "alias": "gemma-4-12b",
        "params": "12B",
        "kv_bytes": 12800,
        "draft_ctx_bytes": 4096,
        "state_mib": 340,
        "max_ctx": 262144,
        "quants": [
            ("UD-Q8_K_XL", 13637327680),
            ("Q8_0",       12669647680),
            ("UD-Q6_K_XL", 10685012800),
            ("UD-Q5_K_XL",  8606021440),
            ("UD-Q4_K_XL",  7366423360),
            ("IQ4_XS",      6375734080),
            ("UD-Q3_K_XL",  6022684480),
            ("UD-IQ3_XXS",  4639716160),
            ("UD-Q2_K_XL",  4661419840),
            ("UD-IQ2_M",    4213353280),
        ],
    }, **_gemma_extras("unsloth/gemma-4-12b-it-GGUF", "gemma-4-12b-it",
                       465109248, 175115840))),
    # Offered, never suggested -- not even to someone running Gemma on
    # a card too small for the 12B, nor by the benchmark. It scores 42%
    # on Tau2 to the 31B's 77%, and tool calls are the job.
    "gemma-e4b": dict(_GEMMA, **dict({
        "label": "Gemma 4 E4B",
        "suggest": False,
        "repo": "unsloth/gemma-4-E4B-it-GGUF",
        "prefix": "gemma-4-E4B-it",
        "alias": "gemma-4-e4b",
        "params": "E4B",
        "kv_bytes": 14848,
        "draft_ctx_bytes": 6144,
        "state_mib": 32,
        "max_ctx": 131072,
        # The per-layer embedding table, at the precision each quant
        # stores it: Q8_0, Q5_K or Q4_K.
        "host_bytes": {
            "UD-Q8_K_XL": 2994733056, "Q8_0": 2994733056,
            "UD-Q6_K_XL": 2994733056, "UD-Q5_K_XL": 2994733056,
            "UD-Q4_K_XL": 1937768448, "IQ4_XS": 1937768448,
            "UD-Q3_K_XL": 1937768448, "UD-IQ3_XXS": 1585446912,
            "UD-Q2_K_XL": 1585446912, "UD-IQ2_M": 1585446912,
        },
        "quants": [
            ("UD-Q8_K_XL", 8711507072),
            ("Q8_0",       8192953472),
            ("UD-Q6_K_XL", 7457762432),
            ("UD-Q5_K_XL", 6656154752),
            ("UD-Q4_K_XL", 5126306944),
            ("IQ4_XS",     4715416704),
            ("UD-Q3_K_XL", 4588440704),
            ("UD-IQ3_XXS", 3717115008),
            ("UD-Q2_K_XL", 3757419648),
            ("UD-IQ2_M",   3545083008),
        ],
    }, **_gemma_extras("unsloth/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it",
                       98653248, 990372672))),
})

# Derived rather than written down twice: a family has MTP or vision
# exactly when it lists a head or a projector to load.
for _fam in MODELS.values():
    _fam["mtp"] = bool(_fam["mtp_heads"])
    _fam["vision"] = bool(_fam["mmproj"])

# Order families are tried in: best first. Only these are ever picked
# for you.
FAMILY_ORDER = ["base", "9b", "4b"]

# Which families a sibling may be suggested from. Running a family
# that cannot clear the bar on this card gets the best sibling that
# can, rather than a jump to a different model line. E4B is left out
# on purpose; see its entry.
GEMMA_ORDER = ["gemma-31b", "gemma-26b", "gemma-12b"]
SIBLINGS = {"qwen": FAMILY_ORDER, "gemma": GEMMA_ORDER}

# The family `kiln start` runs when none is named and several are on
# disk: best first, so it is the one you would have picked by hand.
# Qwen first: Gemma is opt-in, so it runs on its own only when it is
# all there is.
DISK_ORDER = ["base", "ablit", "9b", "4b",
              "gemma-31b", "gemma-26b", "gemma-12b", "gemma-e4b"]

# Named consumer tiers. Purely cosmetic -- nothing branches on the
# name, it just gives the user something to match against the README
# and the hardware reports. The recommendation itself is computed.
TIERS = [
    (46000, "48GB",  "48 GB (A6000 / 6000 Ada / L40S)"),
    (30000, "32GB",  "32 GB (RTX 5090)"),
    (22000, "24GB",  "24 GB (3090 / 4090 / 7900 XTX)"),
    (15000, "16GB",  "16 GB (4060 Ti 16G / 5060 Ti 16G / 9070 XT)"),
    (11000, "12GB",  "12 GB (3060 12G / 5070)"),
    (7000,  "8GB",   "8 GB (3070 / 4060)"),
    (0,     "small", "under 8 GB"),
]


def mib(nbytes):
    """Bytes to MiB, rounded. Matches probe_hardware.ps1's
    [int](Length/1MB) so the two agree to the byte on files both
    have measured -- this repo's numbers were validated with that
    rounding and there is no reason to shift them now."""
    return int(round(nbytes / MIB))


def tier_for(vram_mib):
    for floor, tid, label in TIERS:
        if vram_mib >= floor:
            return tid, label
    return "small", "under 8 GB"


# ---------------------------------------------------------------
#  Detection
# ---------------------------------------------------------------

def _run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def detect_gpu():
    """(vram_mib, name, vendor). vram_mib 0 means 'no idea', which is
    a different situation from 'too small' and gets different advice."""
    # NVIDIA. Multi-GPU: llama.cpp defaults to the largest single
    # device, so that is what gets budgeted -- not the sum.
    out = _run(["nvidia-smi", "--query-gpu=memory.total,name",
                "--format=csv,noheader,nounits"])
    if out:
        best, name = 0, ""
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0].isdigit():
                if int(parts[0]) > best:
                    best, name = int(parts[0]), parts[1]
        if best:
            return best, name, "nvidia"

    # AMD via ROCm. rocm-smi's --showmeminfo prints bytes.
    out = _run(["rocm-smi", "--showmeminfo", "vram", "--csv"])
    if out:
        best = 0
        for m in re.finditer(r"(\d{9,})", out):
            best = max(best, int(m.group(1)))
        if best:
            return mib(best), "AMD GPU (ROCm)", "amd"

    # Apple Silicon: unified memory. Metal will not hand the whole
    # machine to one process -- recommendedMaxWorkingSetSize is
    # ~75% of RAM on Apple Silicon -- so budget that, not total RAM.
    # Intel Macs are not unified: llama.cpp's macOS x64 build has no
    # Metal, so the model runs on the CPU out of RAM. Half of RAM is
    # budgeted for it, leaving the rest to macOS. Apple Silicon is told
    # apart with hw.optional.arm64, not platform.machine(), which says
    # x86_64 for Python running under Rosetta on Apple Silicon.
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "hw.memsize"])
        if out and out.strip().isdigit():
            total = mib(int(out.strip()))
            brand = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
            arm = _run(["sysctl", "-n", "hw.optional.arm64"])
            if not arm or arm.strip() != "1":
                name = (brand or "").strip() or "Intel Mac"
                return int(total * 0.5), name + " (CPU only, no Metal)", "cpu"
            name = "Apple Silicon (unified memory)"
            if brand:
                name = brand.strip() + " (unified memory)"
            return int(total * 0.75), name, "apple"

    return 0, "", "unknown"


def detect_cores():
    """Physical cores. SMT siblings are not extra cores: with every
    layer on the GPU the CPU only feeds batches, so counting them
    adds contention rather than throughput."""
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "hw.physicalcpu"])
        if out and out.strip().isdigit():
            return int(out.strip())
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo") as fh:
                text = fh.read()
            # Distinct (physical id, core id) pairs = physical cores.
            ids, phys, core = set(), None, None
            for line in text.splitlines():
                if line.startswith("physical id"):
                    phys = line.split(":")[1].strip()
                elif line.startswith("core id"):
                    core = line.split(":")[1].strip()
                    if phys is not None:
                        ids.add((phys, core))
            if ids:
                return len(ids)
        except OSError:
            pass
    elif os.name == "nt":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Processor |"
                    " Measure-Object -Property NumberOfCores -Sum).Sum"])
        if out and out.strip().isdigit():
            return int(out.strip())

    # Fall back to half the logical count rather than the whole of it.
    logical = os.cpu_count() or 2
    return max(1, logical // 2)


def detect_ram_mib():
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "hw.memsize"])
        if out and out.strip().isdigit():
            return mib(int(out.strip()))
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) // 1024
        except OSError:
            pass
    elif os.name == "nt":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_ComputerSystem)"
                    ".TotalPhysicalMemory"])
        if out and out.strip().isdigit():
            return mib(int(out.strip()))
    return 0


def detect_free_disk_mib(path="."):
    try:
        return int(shutil.disk_usage(path).free / MIB)
    except OSError:
        return 0


# ---------------------------------------------------------------
#  Sizing
# ---------------------------------------------------------------

def context_for(vram_mib, weights_mib, kv_bytes, state_mib,
                mtp_mib=0, mmproj_mib=0, max_ctx=262144, draft_mib=512,
                draft_ctx_bytes=0):
    """Largest context that fits, or 0 if the fixed costs alone
    exceed VRAM. Same arithmetic the 5090 was validated against."""
    if vram_mib <= 0 or weights_mib <= 0:
        return 0
    free = (vram_mib - DRIVER_RESERVE - weights_mib - mtp_mib - mmproj_mib
            - COMPUTE_BUF_MIB - state_mib
            - (draft_mib if mtp_mib else 0))
    if free <= 0:
        return 0
    per_token = kv_bytes + (draft_ctx_bytes if mtp_mib else 0)
    ctx = int((free * SAFETY * MIB) / per_token)
    ctx = (ctx // CTX_BLOCK) * CTX_BLOCK
    return min(ctx, max_ctx)


def fam_context(fam, vram_mib, weights_mib, mtp_mib=0, mmproj_mib=0,
                max_ctx=None):
    """context_for with a family's own per-token and fixed costs."""
    return context_for(vram_mib, weights_mib, fam["kv_bytes"],
                       fam["state_mib"], mtp_mib, mmproj_mib,
                       min(fam["max_ctx"], max_ctx or fam["max_ctx"]),
                       fam["draft_mib"], fam["draft_ctx_bytes"])


def gpu_mib(fam, quant, nbytes):
    """MiB of a weights file that ends up on the GPU: all of it, except
    what the family keeps in system RAM (E4B's per-layer embeddings)."""
    return mib(nbytes - fam.get("host_bytes", {}).get(quant, 0))


def quant_of(fam, filename):
    """UD-Q5_K_XL out of Qwen3.8-27B-UD-Q5_K_XL.gguf."""
    return os.path.basename(filename)[len(fam["prefix"]) + 1:-len(".gguf")]


def _combos(family_id):
    """Every (mtp_id, mmproj_id) worth trying for a family: each head
    and each projector the family has, best first, and going without."""
    fam = MODELS[family_id]
    return [(m, p) for m in list(fam["mtp_heads"]) + [None]
            for p in list(fam["mmproj"]) + [None]]


# Preference among configurations that all clear the bar. Higher is
# better in every field, compared left to right.
#
# The order is the repo's own reasoning, not a guess:
#
#  1. has MTP        The multi-token-prediction head is the single
#                    biggest generation speedup this architecture has
#                    (1.5-2x). A better quant that gives it up is a
#                    better model that answers more slowly, and for
#                    agentic coding that trade is not worth making.
#  2. quant rank     Below IQ4-class this model gets noticeably weaker
#                    at tool-calling, which is most of what Cursor
#                    asks it to do. See MIN_QUALITY below.
#  3. MTP precision  q8_0 head over q4_0. A real but small effect on
#                    draft acceptance -- worth less than either of the
#                    two above.
#  4. vision         Ranked below all of it. Nobody points Cursor at a
#                    vision tower; it is a bonus, not the job.
#  5. context        Tie-break. Everything here already cleared the
#                    bar, so more is nice rather than necessary.
def _score(fam, quant_idx, mtp_id, mmproj_id, ctx):
    n = len(fam["quants"])
    return (
        1 if mtp_id else 0,
        n - quant_idx,
        {"q8_0": 2, "q4_0": 1}.get(mtp_id, 0),
        {"f16": 2, "q8_0": 1}.get(mmproj_id, 0),
        ctx,
    )


def plan_for(vram_mib, family_id, quant=None, quality_floor=True):
    """Best configuration of one family on this card, or None.

    Searches every (quant x MTP x vision) combination rather than
    walking a fixed order, then ranks the ones that fit by _score.
    An ordered walk cannot express this: the first arrangement that
    happened to clear the bar won, which is how "drop MTP to afford a
    bigger quant" kept winning on a 32 GB card -- the exact opposite
    of what this repo concluded by hand.
    """
    fam = MODELS[family_id]
    floor = fam.get("min_quality", len(fam["quants"]) - 1) if quality_floor         else len(fam["quants"]) - 1

    best, best_key = None, None
    roomiest = None
    for idx, (q, qbytes) in enumerate(fam["quants"]):
        if quant and q != quant:
            continue
        for mtp_id, mmproj_id in _combos(family_id):
            mtp_mib = mib(fam["mtp_heads"][mtp_id].bytes) if mtp_id else 0
            mm_mib = mib(fam["mmproj"][mmproj_id].bytes) if mmproj_id else 0
            ctx = fam_context(fam, vram_mib, gpu_mib(fam, q, qbytes),
                              mtp_mib, mm_mib)
            if ctx == 0:
                continue
            plan = {
                "family": family_id, "quant": q, "quant_bytes": qbytes,
                "mtp": mtp_id, "mmproj": mmproj_id, "ctx": ctx,
            }
            # Anything that loads at all, for the caller that pinned a
            # quant and just wants to know what it would get.
            if roomiest is None or ctx > roomiest["ctx"]:
                roomiest = plan
            if idx > floor or ctx < MIN_CTX:
                continue
            key = _score(fam, idx, mtp_id, mmproj_id, ctx)
            if best_key is None or key > best_key:
                best, best_key = plan, key

    if best:
        return best
    if quant and roomiest:
        # Pinned: report what that choice actually yields, even if it
        # is under the bar. Refusing to answer would be less useful.
        roomiest["below_min"] = True
        return roomiest
    return None


def _roomiest(vram_mib, family_ids):
    """The configuration with the largest window across the quants of
    these families, under the bar or not, or None if nothing loads."""
    roomiest = None
    for fam_id in family_ids:
        for quant, _ in MODELS[fam_id]["quants"]:
            plan = plan_for(vram_mib, fam_id, quant=quant)
            if plan and (roomiest is None or plan["ctx"] > roomiest["ctx"]):
                roomiest = plan
    if roomiest:
        roomiest["below_min"] = True
    return roomiest


def recommend(vram_mib, allow_third_party=True, family=None, quant=None,
              order=None):
    """What this machine should download and run. `order` is the
    families to choose from, best first; FAMILY_ORDER unless given."""
    if family:
        # The same two fallbacks as below, within this family only.
        plan = plan_for(vram_mib, family, quant)
        if not plan:
            plan = plan_for(vram_mib, family, quant, quality_floor=False)
            if plan:
                plan["below_quality"] = True
        # A pinned quant that loads nowhere stays unloadable: another quant
        # would not be the one that was asked for.
        if not plan and not quant:
            plan = _roomiest(vram_mib, [family])
        if plan:
            plan["forced"] = True
        return plan

    families = [f for f in (order or FAMILY_ORDER)
                if allow_third_party or not MODELS[f].get("third_party")]

    # Best family first. Dropping a family is the largest sacrifice
    # available, so it only happens when nothing in the current one
    # clears the bar at an acceptable quant.
    for fam_id in families:
        plan = plan_for(vram_mib, fam_id)
        if plan:
            return plan

    # Nothing cleared the bar anywhere. Relax the quality floor, then
    # the context bar, so the caller gets something to warn about
    # rather than a bare refusal.
    for fam_id in families:
        plan = plan_for(vram_mib, fam_id, quality_floor=False)
        if plan:
            plan["below_quality"] = True
            return plan

    return _roomiest(vram_mib, families)


# ---------------------------------------------------------------
#  Output -- KEY=VALUE so .bat and .sh parse it identically
# ---------------------------------------------------------------

def emit(pairs):
    for k, v in pairs:
        print("%s=%s" % (k, v))


def cmd_detect(args):
    vram, name, vendor = detect_gpu()
    tid, tlabel = tier_for(vram)
    emit([
        ("PROBED", 1 if vram else 0),
        ("VRAM_MIB", vram),
        ("GPU_NAME", name),
        ("GPU_VENDOR", vendor),
        ("CORES", detect_cores()),
        ("RAM_MIB", detect_ram_mib()),
        ("FREE_DISK_MIB", detect_free_disk_mib(args.disk_path)),
        ("TIER", tid),
        ("TIER_LABEL", tlabel),
    ])
    return 0


def cmd_recommend(args):
    vram = args.vram if args.vram else detect_gpu()[0]
    _, name, _ = detect_gpu() if not args.vram else (0, "", "")
    tid, tlabel = tier_for(vram)

    plan = recommend(vram, allow_third_party=not args.no_third_party,
                     family=args.family, quant=args.quant)

    rows = [
        ("PROBED", 1 if vram else 0),
        ("VRAM_MIB", vram),
        ("TIER", tid),
        ("TIER_LABEL", tlabel),
        ("CORES", detect_cores()),
    ]
    if name:
        rows.append(("GPU_NAME", name))

    if not plan:
        # No GPU information at all, or nothing loads. Distinguish
        # them: the advice is completely different.
        rows.append(("REC_OK", 0))
        rows.append(("REC_REASON",
                     "no-gpu" if not vram else "does-not-fit"))
        emit(rows)
        return 0

    fam = MODELS[plan["family"]]
    none = Extra("", "", "", 0)
    head = fam["mtp_heads"][plan["mtp"]] if plan["mtp"] else none
    proj = fam["mmproj"][plan["mmproj"]] if plan["mmproj"] else none
    total = plan["quant_bytes"] + head.bytes + proj.bytes

    rows += [
        ("REC_OK", 1),
        ("REC_FAMILY", plan["family"]),
        ("REC_LABEL", fam["label"]),
        ("REC_REPO", fam["repo"]),
        ("REC_PREFIX", fam["prefix"]),
        ("REC_ALIAS", fam["alias"]),
        ("REC_PARAMS", fam["params"]),
        ("REC_QUANT", plan["quant"]),
        ("REC_QUANT_GB", "%.1f" % (plan["quant_bytes"] / 1e9)),
        ("REC_CTX", plan["ctx"]),
        # _FILE is the name in the repo, _LOCAL the name in models/.
        ("REC_MTP", plan["mtp"] or ""),
        ("REC_MTP_FILE", head.remote),
        ("REC_MTP_LOCAL", head.local),
        ("REC_MTP_REPO", head.repo),
        ("REC_MMPROJ", plan["mmproj"] or ""),
        ("REC_MMPROJ_FILE", proj.remote),
        ("REC_MMPROJ_LOCAL", proj.local),
        ("REC_MMPROJ_REPO", proj.repo),
        ("REC_THIRD_PARTY", 1 if fam.get("third_party") else 0),
        ("REC_TOTAL_GB", "%.1f" % (total / 1e9)),
        ("REC_BELOW_MIN", 1 if plan.get("below_min") else 0),
        ("REC_BELOW_QUALITY", 1 if plan.get("below_quality") else 0),
        ("REC_MIN_CTX", MIN_CTX),
    ]
    emit(rows)
    return 0


def family_of(filename):
    """The family a weights file belongs to, by its name, or None.
    Longest prefix first, so "Huihui-Qwen3.8-27B-abliterated" is not
    shadowed by a shorter prefix that is a substring of it."""
    for fid in sorted(MODELS, key=lambda f: -len(MODELS[f]["prefix"])):
        if filename.startswith(MODELS[fid]["prefix"] + "-"):
            return fid
    return None


def cmd_size_model(args):
    """Context for a file already on disk. This is the path start.*
    takes: the weights are chosen, only the window is in question."""
    if not os.path.isfile(args.size_model):
        emit([("PROBED", 0), ("CTX", 0), ("ERROR", "no-such-file")])
        return 1

    base = os.path.basename(args.size_model)
    fam_id = family_of(base) or "base"
    fam = MODELS[fam_id]
    vram = args.vram if args.vram else detect_gpu()[0]
    weights = gpu_mib(fam, quant_of(fam, base),
                      os.path.getsize(args.size_model))
    mtp_mib = mib(os.path.getsize(args.mtp)) if args.mtp and os.path.isfile(args.mtp) else 0
    mm_mib = mib(os.path.getsize(args.mmproj)) if args.mmproj and os.path.isfile(args.mmproj) else 0

    ctx = fam_context(fam, vram, weights, mtp_mib, mm_mib, args.max_ctx)
    tid, tlabel = tier_for(vram)

    rows = [
        ("PROBED", 1 if vram else 0),
        ("VRAM_MIB", vram),
        ("CORES", detect_cores()),
        ("CTX", ctx),
        ("WEIGHTS_MIB", weights),
        ("FAMILY", fam_id),
        ("KV_BYTES", fam["kv_bytes"]),
        ("TIER", tid),
        ("TIER_LABEL", tlabel),
    ]

    # Would this machine have chosen a different QUANT of the same
    # family? The caller prints that as a note; it never overrides
    # what is on disk.
    #
    # Deliberately compared within the family, not across all of
    # them. Running the abliterated build instead of the stock one
    # is a choice about the model, not a mistake about the card, and
    # nagging about it every launch would train the user to ignore
    # the line that matters.
    best = plan_for(vram, fam_id) if vram else None
    if best:
        rows += [
            # Exact, not a substring: Q6_K is inside UD-Q6_K_XL.
            ("REC_SAME", 1 if base == "%s-%s.gguf" % (fam["prefix"],
                                                       best["quant"]) else 0),
            ("REC_FAMILY", fam_id),
            ("REC_QUANT", best["quant"]),
            ("REC_CTX", best["ctx"]),
            ("REC_MTP", best["mtp"] or ""),
        ]
    elif vram:
        # Nothing in this family clears the bar on this card. That IS
        # worth crossing families for, so point at the one that does:
        # a sibling that clears the bar, else whatever kiln would pick
        # on its own.
        alt = next((p for p in (plan_for(vram, f)
                                for f in SIBLINGS.get(fam["group"], ()))
                    if p), None) or recommend(vram)
        if alt and alt["family"] != fam_id:
            rows += [
                ("REC_SAME", 0),
                ("REC_FAMILY", alt["family"]),
                ("REC_QUANT", alt["quant"]),
                ("REC_CTX", alt["ctx"]),
                ("REC_MTP", alt["mtp"] or ""),
            ]
    emit(rows)
    return 0


def cmd_variant(args):
    """The fields lib_variants.bat / lib_variants.sh set, straight
    from the registry. Those two keep a hardcoded base/ablit copy as
    a no-Python fallback, but every other family is only defined
    here, so there is one place to add the next one."""
    fam_id = args.variant
    aliases = {"stock": "base", "abliterated": "ablit",
               "9B": "9b", "4B": "4b"}
    fam_id = aliases.get(fam_id, fam_id)
    if fam_id not in MODELS:
        print("ERROR=unknown-variant", file=sys.stderr)
        return 1
    fam = MODELS[fam_id]
    emit([
        ("V_ID", fam_id),
        ("V_LABEL", fam["label"]),
        ("V_REPO", fam["repo"]),
        ("V_PREFIX", fam["prefix"]),
        ("V_ALIAS", fam["alias"]),
        ("V_UNCENSORED", 1 if fam["uncensored"] else 0),
        ("V_THIRD_PARTY", 1 if fam.get("third_party") else 0),
        ("V_MTP", 1 if fam["mtp"] else 0),
        ("V_VISION", 1 if fam["vision"] else 0),
        ("V_PARAMS", fam["params"]),
        # Best quant first -- start.* uses this to pick among the
        # files actually on disk.
        ("V_QUANTS", " ".join(q for q, _ in fam["quants"])),
        # The same, for the MTP head and the projector: local names
        # in models/, best first.
        ("V_MTP_FILES", " ".join(h.local for h in fam["mtp_heads"].values())),
        ("V_MMPROJ_FILES", " ".join(p.local for p in fam["mmproj"].values())),
        ("V_SPEC_N", fam["spec_n_max"]),
        ("V_SAMPLING", fam["sampling"]),
        ("V_REASONING", fam["reasoning"]),
        ("V_MIN_BUILD", fam["min_build"]),
    ])
    return 0


def llama_build(exe):
    """The build number of a llama-server binary, or 0 if it cannot be
    read. Newer builds print "version: 0.4.0-dev (build 10901, commit
    ...)", older ones "version: 9431 (94ca829b6)". Never match a bare
    "build": the "built with Clang" line would answer -- see NOTES.md."""
    # Not _run: --version prints to stderr, which that does not keep.
    try:
        proc = subprocess.run([exe, "--version"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True,
                              errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 0
    out = proc.stdout + proc.stderr
    m = re.search(r"\(build (\d+)", out) or re.search(r"version:\s*(\d+)", out)
    return int(m.group(1)) if m else 0


def cmd_build(args):
    emit([("BUILD", llama_build(args.build))])
    return 0


def on_disk(fam_id, models_dir=MODELS_DIR):
    """The best quant of a family present in models_dir, or ""."""
    fam = MODELS[fam_id]
    return next((q for q, _ in fam["quants"]
                 if os.path.isfile(os.path.join(
                     models_dir, "%s-%s.gguf" % (fam["prefix"], q)))), "")


def cmd_pick_on_disk(args):
    """The family `kiln start` runs when none is named. Empty when
    nothing is downloaded, so the caller names its own default."""
    emit([("VARIANT", next((f for f in DISK_ORDER
                            if on_disk(f, args.models_dir)), ""))])
    return 0


def cmd_quants(args):
    """One line per quant in a family, sized against THIS card, for
    the download menu to render:

        QUANT|GB|CTX|MTP|MMPROJ|FITS|RECOMMENDED

    The menu used to carry its own hardcoded GB column, which meant
    the sizes it showed and the sizes the free-space check used could
    drift apart. Both now come from here.
    """
    fam_id = args.quants
    if fam_id not in MODELS:
        print("ERROR=unknown-variant", file=sys.stderr)
        return 1
    fam = MODELS[fam_id]
    vram = args.vram if args.vram else detect_gpu()[0]

    rec = recommend(vram) if vram else None
    rec_quant = rec["quant"] if rec and rec["family"] == fam_id else None
    # If the best family for this card is not the one being browsed,
    # still mark the best quant WITHIN this family, so the menu always
    # has a sensible default.
    if rec_quant is None and vram:
        own = recommend(vram, family=fam_id)
        rec_quant = own["quant"] if own else None

    for quant, qbytes in fam["quants"]:
        plan = plan_for(vram, fam_id, quant=quant) if vram else None
        ctx = plan["ctx"] if plan else 0
        mtp = (plan["mtp"] or "off") if plan else "-"
        mm = (plan["mmproj"] or "off") if plan else "-"
        print("%s|%.1f|%d|%s|%s|%d|%d" % (
            quant, qbytes / 1e9, ctx, mtp, mm,
            1 if ctx else 0,
            1 if quant == rec_quant else 0))
    return 0


TABLE_CARDS = [(12288, "12 GB"), (16384, "16 GB"), (24564, "24 GB"),
               (32607, "32 GB"), (49140, "48 GB")]


def _fmt_ctx(ctx):
    return "%dK" % (ctx // 1024) if ctx else "—"


def cmd_table(args):
    """The README matrix, generated from the same arithmetic the
    launcher uses -- so the docs cannot drift from the code.
    --table gemma prints the Gemma 4 section instead."""
    if args.table == "gemma":
        return _gemma_table()
    _matrices(FAMILY_ORDER)

    # The matrix above fixes the MTP head at q8_0 and vision at f16.
    # The recommendation below is free to take the smaller q4_0 head
    # or drop vision, so it can legitimately show a larger window than
    # the cell it came from. Say so rather than leaving the reader to
    # discover the two tables disagree.
    print("\nEvery cell above assumes the q8_0 MTP head and the f16 vision")
    print("projector. The recommendation below may take the smaller q4_0")
    print("head (-1.4 GB) or drop vision, so it can show a larger window")
    print("than the matching cell above.")

    print("\n**What each card is recommended**\n")
    print("| Card | Tier | Model | Quant | MTP | Vision | Context |")
    print("|---|---|---|---|---|---|---|")
    for vram, label in TABLE_CARDS:
        plan = recommend(vram)
        tid, _ = tier_for(vram)
        if not plan:
            print("| %s | %s | — | — | — | — | does not fit |"
                  % (label, tid))
            continue
        fam = MODELS[plan["family"]]
        print("| %s | %s | %s | `%s` | %s | %s | %s |" % (
            label, tid, fam["label"], plan["quant"],
            plan["mtp"] or "off", plan["mmproj"] or "off",
            _fmt_ctx(plan["ctx"])))
    return 0


def _gemma_table():
    _matrices(GEMMA_ORDER + ["gemma-e4b"])
    # Only the families a Gemma user is ever pointed at: E4B is left
    # out here as it is from SIBLINGS.
    print("\n**What each card runs, if you ask for Gemma 4**\n")
    print("| Card | Model | Quant | MTP | Vision | Context |")
    print("|---|---|---|---|---|---|")
    for vram, label in TABLE_CARDS:
        plan = next((p for p in (plan_for(vram, f) for f in GEMMA_ORDER)
                     if p), None)
        if not plan:
            print("| %s | — | — | — | — | does not fit |" % label)
            continue
        print("| %s | %s | `%s` | %s | %s | %s |" % (
            label, MODELS[plan["family"]]["label"], plan["quant"],
            plan["mtp"] or "off", plan["mmproj"] or "off",
            _fmt_ctx(plan["ctx"])))
    return 0


def _matrices(fam_ids):
    cards = TABLE_CARDS
    fmt = _fmt_ctx
    for fam_id in fam_ids:
        fam = MODELS[fam_id]
        # The best head and projector: q8_0 and f16 in every family.
        mtp = next(iter(fam["mtp_heads"].items()), None)
        mm = next(iter(fam["mmproj"].items()), None)
        extra = []
        if mtp:
            extra.append("MTP %s" % mtp[0])
        if mm:
            extra.append("vision %s" % mm[0])
        note = (" (with %s)" % " + ".join(extra)) if extra else " (text-only)"
        print("\n**%s**%s\n" % (fam["label"], note))
        print("| Quant | " + " | ".join(c[1] for c in cards) + " |")
        print("|---|" + "---|" * len(cards))
        for quant, qbytes in fam["quants"]:
            cells = []
            for vram, _ in cards:
                ctx = fam_context(fam, vram, gpu_mib(fam, quant, qbytes),
                                  mib(mtp[1].bytes) if mtp else 0,
                                  mib(mm[1].bytes) if mm else 0)
                cells.append(fmt(ctx))
            print("| `%s` | %s |" % (quant, " | ".join(cells)))


def main():
    # The table is pasted straight into README.md, which is UTF-8.
    # A Windows console defaults to cp1252 and turns the em-dash into
    # a replacement byte, so say what we mean.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    p = argparse.ArgumentParser(add_help=True, description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--detect", action="store_true")
    g.add_argument("--recommend", action="store_true")
    g.add_argument("--size-model", metavar="PATH")
    g.add_argument("--table", nargs="?", const="qwen",
                   choices=["qwen", "gemma"],
                   help="the README matrix; gemma for the Gemma 4 one")
    g.add_argument("--variant", metavar="ID",
                   help="emit V_* registry fields for one family")
    g.add_argument("--quants", metavar="ID",
                   help="per-quant sizes and contexts for one family")
    g.add_argument("--pick-on-disk", action="store_true",
                   help="the family kiln start runs when none is named")
    g.add_argument("--build", metavar="EXE",
                   help="the build number of a llama-server binary")

    p.add_argument("--vram", type=int, default=0,
                   help="override detected VRAM in MiB (for tables/testing)")
    p.add_argument("--family", choices=list(MODELS),
                   help="pin the model family instead of choosing one")
    p.add_argument("--quant", help="pin the quant instead of choosing one")
    p.add_argument("--no-third-party", action="store_true",
                   help="only recommend official Qwen weights")
    p.add_argument("--mtp", default="", help="MTP head path, for --size-model")
    p.add_argument("--mmproj", default="", help="mmproj path, for --size-model")
    p.add_argument("--max-ctx", type=int, default=262144)
    p.add_argument("--disk-path", default=".")
    p.add_argument("--models-dir", default=MODELS_DIR,
                   help="where to look, for --pick-on-disk")
    args = p.parse_args()

    if args.detect:
        return cmd_detect(args)
    if args.pick_on_disk:
        return cmd_pick_on_disk(args)
    if args.build:
        return cmd_build(args)
    if args.recommend:
        return cmd_recommend(args)
    if args.table:
        return cmd_table(args)
    if args.variant:
        return cmd_variant(args)
    if args.quants:
        return cmd_quants(args)
    return cmd_size_model(args)


if __name__ == "__main__":
    sys.exit(main())
