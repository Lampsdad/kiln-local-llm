# Local LLM setup for Cursor IDE

**Run Qwen3.8-27B locally on your own GPU and use it inside Cursor as a
free, private, drop-in replacement for GPT and Claude.** A self-hosted,
OpenAI-compatible llama.cpp server with MTP speculative decoding, vision,
and a 112K context window on a single 32 GB card — no subscription, no
code leaving your machine. Stock or abliterated, picked at launch.

[![CI](https://github.com/Lampsdad/kiln-local-llm/actions/workflows/ci.yml/badge.svg)](https://github.com/Lampsdad/kiln-local-llm/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/Lampsdad/kiln-local-llm?style=flat)](https://github.com/Lampsdad/kiln-local-llm/stargazers)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![llama.cpp](https://img.shields.io/badge/llama.cpp-b9180%2B-orange)
![Model](https://img.shields.io/badge/model-Qwen3.8--27B-purple)

```bat
kiln
```

One command. Bare `kiln` opens the full-screen TUI. `kiln status`
prints what is installed, downloaded and running. macOS and Linux use
the same front door: `./kiln.sh` (same verbs). Setup puts `kiln` on
your PATH through `~/.local/bin`, on Windows too, so it runs from any
folder.

**Contents** — [Why not Ollama?](#why-not-just-use-ollama-or-lm-studio) ·
[Will this run on my GPU?](#will-this-run-on-my-gpu) ·
[Quick start](#quick-start) ·
[Why Qwen3.8 is different](#what-makes-qwen38-27b-different) ·
[Choosing a build](#choosing-a-build) ·
[Gemma 4 (opt-in)](#gemma-4-opt-in) ·
[Choosing a quant](#choosing-a-quant) ·
[Using with Cursor](#using-with-cursor) ·
[Using with OpenCode](#using-with-opencode) ·
[Using with pi](#using-with-pi) ·
[Security](#security) ·
[Scripts](#scripts) ·
[Tuned settings](#tuned-server-settings) ·
[Troubleshooting](#troubleshooting) ·
[FAQ](#faq)

---

## Why not just use Ollama or LM Studio?

Use those if you want any model running in two minutes. Use this if you
want *this* model running *well*:

- **MTP speculative decoding, actually wired up.** Qwen trains
  multi-token-prediction heads into Qwen3.8 and ships them as a separate
  GGUF. Loaded as a draft model, it is the single largest generation
  speedup available for this architecture. Most front-ends do not expose
  `--spec-type draft-mtp` at all.
- **Settings derived from the architecture, not copied from a 7B guide.**
  Qwen3.8 is 75% linear attention, which changes what KV precision and
  context sizing should be. The reasoning is written down below.
- **Sized to your actual GPU, before you download 21 GB.** `kiln
  hardware` reads your VRAM and picks the model, quant, MTP head and
  context window that fit, rather than assuming a card you may not own.
  On a 12 GB card that means a different *model*, not just a smaller
  context.
- **A measurement path, not just claims.** `kiln bench`
  (`benchmark.{bat,sh}`) and `kiln quality` (`quality.{bat,sh}`) let you
  check every tuning decision here against your own hardware and your
  own copies of the files.
- **Remote access built in.** A Cloudflare tunnel plus a generated API
  key, so you can point Cursor at your desktop GPU from a laptop.

---

## Will this run on my GPU?

Ask it:

```
kiln hardware          # Windows
./kiln.sh hardware     # macOS / Linux
```

It reads your GPU, picks the model, quant, MTP head and context window
that fit, and prints exactly what `kiln get` would download. Everything
below is that same calculation, run ahead of time for common cards — you
should not need to read it.

**Qwen3.8-27B** (with MTP q8_0 + vision f16)

| Quant | 12 GB | 16 GB | 24 GB | 32 GB | 48 GB |
|---|---|---|---|---|---|
| `UD-Q8_K_XL` | — | — | — | — | 256K |
| `Q8_0` | — | — | — | — | 256K |
| `UD-Q6_K_XL` | — | — | — | 8K | 256K |
| `UD-Q5_K_XL` | — | — | — | 112K | 256K |
| `UD-Q4_K_XL` | — | — | — | 192K | 256K |
| `UD-IQ4_XS` | — | — | 68K | 256K | 256K |
| `UD-Q3_K_XL` | — | — | 96K | 256K | 256K |
| `UD-IQ3_S` | — | — | 120K | 256K | 256K |
| `UD-IQ3_XXS` | — | — | 148K | 256K | 256K |
| `UD-Q2_K_XL` | — | — | 172K | 256K | 256K |
| `UD-IQ2_S` | — | 4K | 208K | 256K | 256K |

**Qwen3.8-9B distill** (text-only)

| Quant | 12 GB | 16 GB | 24 GB | 32 GB | 48 GB |
|---|---|---|---|---|---|
| `Q8_0` | — | 168K | 256K | 256K | 256K |
| `Q6_K` | 68K | 256K | 256K | 256K | 256K |
| `Q5_K_M` | 112K | 256K | 256K | 256K | 256K |
| `Q4_K_M` | 156K | 256K | 256K | 256K | 256K |

**Qwen3.8-4B distill** (text-only)

| Quant | 12 GB | 16 GB | 24 GB | 32 GB | 48 GB |
|---|---|---|---|---|---|
| `Q8_0` | 212K | 256K | 256K | 256K | 256K |
| `Q6_K` | 256K | 256K | 256K | 256K | 256K |
| `Q5_K_M` | 256K | 256K | 256K | 256K | 256K |
| `Q4_K_M` | 256K | 256K | 256K | 256K | 256K |

Every cell above assumes the q8_0 MTP head and the f16 vision
projector. The recommendation below may take the smaller q4_0
head (-1.4 GB) or drop vision, so it can show a larger window
than the matching cell above.

**What each card is recommended**

| Card | Tier | Model | Quant | MTP | Vision | Context |
|---|---|---|---|---|---|---|
| 12 GB | 12GB | Qwen3.8-9B distill | `Q5_K_M` | off | off | 112K |
| 16 GB | 16GB | Qwen3.8-9B distill | `Q8_0` | off | off | 168K |
| 24 GB | 24GB | Qwen3.8-27B | `UD-IQ4_XS` | q4_0 | f16 | 104K |
| 32 GB | 32GB | Qwen3.8-27B | `UD-Q5_K_XL` | q8_0 | f16 | 112K |
| 48 GB | 48GB | Qwen3.8-27B | `UD-Q8_K_XL` | q8_0 | f16 | 256K |

**Reading this:**

- **48 GB+** — everything fits at the model's full 256K.
- **32 GB (5090 / A6000)** — the tuned target. `UD-Q5_K_XL` with the
  q8_0 MTP head and vision at 112K, and no configuration needed.
- **24 GB (3090 / 4090 / 7900 XTX)** — `UD-IQ4_XS` with the **q4_0** MTP
  head. Earlier versions of this README told you to give up MTP here;
  that was wrong. The smaller head costs 1.4 GB instead of 3.0 GB and
  keeps the 1.5–2x speedup at a better quant than dropping it would buy.
- **16 GB and below** — a 27B is the wrong model for the card. Below
  `UD-IQ4_XS` it gets noticeably worse at the tool-calling that agentic
  coding is mostly made of, so `kiln` moves you to a smaller model at a
  good quant rather than a big one at a bad one.

### The smaller models

Qwen shipped no small dense sibling in the 3.8 family — the official
lineup is 27B, Flash-Next (a 180B MoE) and 2.4T-A95B. So for 16 GB and
under, `kiln` offers two **third-party** builds:

| Build | What it is | License |
|---|---|---|
| `9b` | [empero-ai/Qwen3.8-9B-Distill-GGUF](https://huggingface.co/empero-ai/Qwen3.8-9B-Distill-GGUF) — Qwen3.8-2.4T-A95B distilled into the Qwen3.5-9B architecture | Apache-2.0 |
| `4b` | [empero-ai/Qwen3.8-4B-Distill-GGUF](https://huggingface.co/empero-ai/Qwen3.8-4B-Distill-GGUF) — the same, at 4B | Apache-2.0 |

These are **not Qwen releases** and have not been evaluated here. Both
are text-only: their GGUF repos ship no vision projector and no MTP
head, so vision and the speculative-decoding speedup are unavailable
whatever card you run them on. Every surface that offers them says so.
They are still the better answer than a 27B at 2 bits.

### Trading context against quality

`kiln` maximises quant quality subject to clearing a context floor,
which defaults to 96K — enough for agentic coding, and the bar that
reproduces the hand-validated 32 GB configuration. Move it if your job
is different:

```
KILN_MIN_CTX=32768  kiln hardware    # short prompts, best quant
KILN_MIN_CTX=200000 kiln hardware    # window over everything
```

---

## Quick start

### Windows

```bat
git clone https://github.com/Lampsdad/kiln-local-llm
cd kiln-local-llm
kiln
```

That is the whole install. Bare `kiln` opens the full-screen TUI.
`kiln status` prints the status board, which tells you the one thing
to do next:

```
  _     _  _        
 | | __(_)| | _ __  
 | |/ /| || || '_ \ 
 |   < | || || | | |
 |_|\_\|_||_||_| |_|  local models, fired on your own GPU

 ------------------------------------------------------------
  kiln           v1.1.0
  llama.cpp      installed  build 9431
  MTP support    available
  cloudflared    installed
  GPU            NVIDIA GeForce RTX 5090, 32607 MiB
  disk free      392 GB
 ------------------------------------------------------------
  Qwen3.8-27B    UD-Q5_K_XL
   abliterated   UD-Q5_K_XL
  shared files   MTP head   vision
 ------------------------------------------------------------
  server         stopped
  API key        xxxxxx...xxxx   kiln key show
  tunnel         none
 ------------------------------------------------------------

 Next:  kiln start    serve a model to Cursor
```

The `Next:` line is the first unmet dependency, so following it
repeatedly walks you from an empty checkout to a served model. Colour is
automatic and honours `NO_COLOR`.

Every step is also a verb, if you would rather drive it yourself:

```bat
kiln status       :: just the board
kiln setup        :: llama.cpp, cloudflared, models
kiln update       :: upgrade llama.cpp (needed for MTP)
kiln get both     :: download the stock and abliterated weights
kiln start        :: pick a build and serve it
kiln start ablit  :: skip the picker
kiln stop         :: stop the server and the tunnel
kiln key show     :: print the API key for Cursor
kiln opencode     :: list the local models in OpenCode
kiln pi           :: list the local models in pi
kiln bench        :: quant speed and throughput sweep
kiln clean        :: reclaim disk from superseded GGUFs
kiln self-update  :: update kiln itself to the newest release
kiln tui          :: all of the above as a full-screen app
kiln path         :: put kiln on PATH, so it runs from any folder
```

`kiln setup` puts `kiln` on your PATH. It writes a small `kiln.cmd`
into `%USERPROFILE%\.local\bin` and adds that folder to your user PATH,
with no admin rights. Open a new terminal and `kiln` works from any
folder. `kiln path` does only that step, and `kiln path --remove` undoes
it.

### macOS and Linux

```bash
git clone https://github.com/Lampsdad/kiln-local-llm
cd kiln-local-llm
./kiln.sh                 # full-screen TUI
```

Bare `./kiln.sh` opens the TUI. `./kiln.sh status` prints the status
board. `./kiln.sh setup` links `kiln` into `~/.local/bin` (no sudo), so
the same commands then work from any directory as `kiln`. If that
folder is not on your PATH yet, setup prints the line that adds it.
`./kiln.sh path` does only the linking, and `--remove` undoes it.
Same verbs, same order. `kiln.sh` detects macOS, Debian/Ubuntu or
Fedora/RHEL and calls the right platform script for you:

```bash
./kiln.sh status          # print the status board
./kiln.sh setup           # llama.cpp, cloudflared, models/; links kiln onto PATH
./kiln.sh hardware        # what this GPU can run, and at what context
./kiln.sh get both        # download the stock and abliterated weights
./kiln.sh start           # serve a model to Cursor
./kiln.sh start ablit     # or the abliterated build
./kiln.sh stop
./kiln.sh key show
./kiln.sh opencode        # list the local models in OpenCode
./kiln.sh pi              # list the local models in pi
./kiln.sh bench           # quant speed and throughput sweep
./kiln.sh quality         # KL-divergence of each quant vs Q8_0
./kiln.sh self-update     # update kiln itself to the newest release
./kiln.sh start --no-mtp  # trade the MTP head for a larger window
./kiln.sh tui             # full-screen app, which is also the default
```

Override the quant with an environment variable:

```bash
QUANT=UD-Q4_K_XL ./kiln.sh get
```

All platforms support MTP, vision, both builds and the generated API
key. The shell path sizes context with llama.cpp's `--fit on` rather
than the Windows probe, so no table lookup is needed there.
`scripts/unix/slurm-llama.sh` runs the same server as a Slurm batch job
so the GPU shows as allocated in `squeue`.

### The full-screen app

```bat
kiln tui
```

Bare `kiln` opens this app. `kiln status` prints the board. `kiln tui`
(`./kiln.sh tui`) is the same full-screen view of the status board. It shows what is installed, downloaded and running, the
URL and key Cursor needs, what each model would cost on this GPU, and
the same `Next:` step the board gives. Each key runs the matching kiln
command and shows its output as it happens:

```
n  next step     s  start      x  stop        d  download
b  benchmark     k  copy key   u  copy URL    ?  every key
m  start with or without the MTP head   l  with or without the tunnel
```

A model you start from it keeps serving after you quit. It opens no
network port, so it works over SSH too.

It needs one library that is not part of Python:
[Textual](https://textual.textualize.io). The first `kiln tui` offers
to install it into `.kiln/` inside this folder and nowhere else. Delete
that folder to remove it. It needs Python 3.9 or newer. The rest of
kiln still runs on 3.8.

### Finding the right setup for your GPU

Press `b` in `kiln tui` to benchmark. It starts each downloaded model
the way `kiln start` would, with and without the MTP head, and times
real requests against it. The abliterated build is listed but not
ticked unless it is the one you run: it is the same size and speed as
the stock model, so measuring both tells you nothing about your GPU.

| Column | What it measures |
|---|---|
| Context | the window the server actually opened |
| Load | seconds from start to serving |
| Prefill t/s | how fast it reads an 8K-token prompt |
| Gen t/s | how fast it writes, which is the speed you notice in Cursor |
| MTP accept | the share of drafted tokens the model kept |
| Deep t/s | thorough runs only: writing speed with 32K tokens already in context |
| VRAM | memory in use once loaded |

It recommends the fastest setup that still opens at least 96K of context
(`KILN_MIN_CTX` moves that bar), because Cursor's agent sends tens of
thousands of tokens. MTP speed swings from run to run with how
predictable the text is, so when another setup's runs overlap the
pick's, it says the two are a tie rather than crowning one. Choose any
row and press `a` to start it. It also becomes the default for `s`. A
quick run takes about a minute and a half per setup. The tunnel stays
off while it measures, and the server is stopped when it finishes.

`p` copies the results as a markdown table, ready to paste into a
[hardware report](https://github.com/Lampsdad/kiln-local-llm/issues/new?template=hardware_report.yml).
The results are saved in `kiln-benchmark.json`. The same benchmark runs
without the app:

```
python scripts/bench.py --list       # the setups on this machine
python scripts/bench.py              # measure the ones --list marks
python scripts/bench.py base-nomtp   # or name them
python scripts/bench.py --report     # the last results, as markdown
```

`kiln bench` is a different tool. It sweeps quants, batch sizes and KV
precision with llama-bench, which cannot run speculative decoding, so it
does not show what MTP is worth.

### Keeping kiln up to date

```bat
kiln self-update            :: move to the newest release
kiln self-update --check    :: only say whether there is one
kiln self-update --main     :: follow every change on main instead
kiln self-update --release  :: go back to releases
```

`./kiln.sh self-update` takes the same flags. This is not the same as
`kiln update`, which upgrades llama.cpp.

By default kiln follows **releases**. It moves only when a new `vX.Y.Z`
is tagged, never to work in progress on `main`. With `--main` you get
every change the day it lands, and that choice is remembered until
`--release` switches back.

The update is a git fast-forward, so `models/`, `llama-bin/` and
`api_key.txt` are never touched. When it cannot update safely, it
changes nothing and says why. That happens when a tracked file has local
edits (it shows how to `git stash` them), when `main` has commits of
your own, or when another branch is checked out. It needs the
`git clone` install, because a "Download ZIP" copy has no history to
update from. If a server is running, restart it afterwards.

The first row of the status board shows the version you are on and
says when a newer release is out. To find out, it asks GitHub once a day
with `git ls-remote`, which downloads nothing, and gives up after a few
seconds when offline. Set `KILN_NO_UPDATE_CHECK=1` to turn the check off.

If you cloned before `self-update` existed, run `git pull` once to
get it.

---

## What makes Qwen3.8-27B different

Three architectural facts drive nearly every setting in this repo.

**1. It is 75% linear attention.** The layer stack is
`16 × (3 × GatedDeltaNet → 1 × GatedAttention)` — 64 layers total, of
which only **16 hold a KV cache**. The other 48 carry a small fixed-size
recurrent state.

Two consequences:

- **Long context is unusually cheap.** KV cost is
  `16 layers × 4 KV heads × 256 head-dim × 2 (K+V)` = 32,768
  elements/token → **64 KiB/token at f16, 34 KiB at q8_0**. A comparable
  dense transformer with all 64 layers cached would cost four times as
  much. This is why the tables above show 112K where a normal 27B would
  give you 16K.
- **Low-bit KV hurts more than usual.** In recurrent layers, error
  accumulates *along the sequence* instead of being re-anchored against a
  cache each token. This repo uses `q8_0` KV, not the `q4_0` that older
  Qwen3.6 setups used.

**2. MTP is trained into the weights but ships as a separate file.**
Qwen trains multi-token-prediction heads directly into the model. In
llama.cpp these load as a *draft model* for speculative decoding:

```
--spec-type draft-mtp --spec-draft-model models/mtp-Qwen3.8-27B-Q8_0.gguf --spec-draft-n-max 3
```

This needs **llama.cpp b9180 or newer**
([PR #22673](https://github.com/ggml-org/llama.cpp/pull/22673), merged
2026-05-16). Older builds reject the flag outright. The MTP head is only
published by `ggml-org/Qwen3.8-27B-GGUF`, so it is paired with unsloth
weights — both are conversions of the same upstream checkpoint, so vocab
and hidden dims match.

**3. The vocab is enormous — 248,320 tokens, untied embeddings.** That is
`248320 × 5120` for the input embedding *and* again for the output head:
roughly 2.5B params, ~9% of the model, concentrated in two tensors. Naive
quants crush them.

> **Use Unsloth `UD-*_XL` quants.** The `_XL` suffix means embed/output
> are kept at higher precision. This is why `UD-Q4_K_XL` (17.9 GB) is
> *larger* than plain `Q4_K_M` (17.1 GB) — and on a vocab this size, that
> difference does real work.

It is also a native VLM (images and video), so `--mmproj` is wired up by
default.

---

## Choosing a build

Two variants of the 27B are wired up. `kiln start` shows both, marks
which are on disk, and downloads the one you pick. (The smaller
distills are [above](#the-smaller-models), and Gemma 4 is
[below](#gemma-4-opt-in).)

| | Stock | Abliterated |
|---|---|---|
| Weights | [`unsloth/Qwen3.8-27B-GGUF`](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF) | [`huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF`](https://huggingface.co/huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF) |
| Cursor model name | `qwen3.8-27b` | `qwen3.8-27b-abliterated` |
| Refusal behaviour | as Qwen shipped it | ablated |
| Weights on disk | ~20 GB | ~21 GB |
| `kiln start` argument | `base` | `ablit` |

**They cost less together than apart.** huihui-ai's `UD-*` quants are
re-ablations of the same unsloth GGUFs this repo already used, with the
ablation confined to layers 18–51 and the MTP head and vision tower left
alone. So both builds share one copy of `mtp-Qwen3.8-27B-Q8_0.gguf`
(3.2 GB) and `mmproj-F16.gguf` (0.93 GB) — the second build you download
is weights-only. It also means the quant names, the file sizes, and the
context table above apply unchanged to both.

**Only one runs at a time.** They share port 8080, and the start script
stops any running server before starting the next, so `kiln start` is a
switch rather than a way to serve both at once. The distinct `--alias`
values are what keep them apart in Cursor's model list — you will not
silently be talking to the other one.

**What abliteration actually costs.** It is refusal-direction ablation,
not fine-tuning: the refusal direction is projected out of the residual
stream. Keeping layers 0–17 untouched is what preserves most of the
coding and tool-calling ability, but this is still a modified model with
no published evaluation of its behaviour, and it will answer things the
stock build declines. If you expose it through the Cloudflare tunnel,
the generated API key is the only thing between it and the open
internet — see [Security](#security).

Reach for the stock build unless you have a specific reason not to; it is
what the tuning numbers below were measured on.

---

## Gemma 4 (opt-in)

Google's [Gemma 4](https://huggingface.co/collections/google/gemma-4)
is wired up as a second model line. **kiln never picks it for you.**
`kiln hardware`, a bare `kiln get` and a bare `kiln start` all stay on
Qwen3.8. Gemma runs when you name it:

```bat
kiln get gemma-26b        :: sized to this GPU, like any other build
kiln start gemma-26b
```

| Variant | Model | What it is | Cursor model name |
|---|---|---|---|
| `gemma-31b` | Gemma 4 31B | dense; the quality pick for 32 GB and up | `gemma-4-31b` |
| `gemma-26b` | Gemma 4 26B-A4B | mixture of experts, 3.8B active per token; the 24 GB pick | `gemma-4-26b-a4b` |
| `gemma-12b` | Gemma 4 12B | dense; for 12–16 GB | `gemma-4-12b` |
| `gemma-e4b` | Gemma 4 E4B | small and quick, but weak at tool calls | `gemma-4-e4b` |

All four come from unsloth's GGUF repos (Apache-2.0). Each has its own
MTP draft head (Google's "assistant" drafter) and its own vision
projector. Nothing is shared with the Qwen builds. E4B is there if you
ask for it, but kiln never suggests it, even to someone running Gemma
on a card too small for the 12B. It scores 42% on Tau2 to the 31B's 77%,
and tool calls are most of what Cursor sends.

**It needs llama.cpp b11059 or newer.** On Windows that is
`kiln update`. On macOS and Linux, `mv llama-bin llama-bin-backup &&
./kiln.sh setup`, which keeps the old build in `llama-bin-backup/`.
`kiln start` refuses an older build, and does so before it stops the
server you are running. Gemma's MTP support is older than that, but the
fixes for its draft head (b10742), its vision tower (b10798) and
required tool calls (b11059) came later.

**What each card runs, if you ask for Gemma 4**

| Card | Model | Quant | MTP | Vision | Context |
|---|---|---|---|---|---|
| 12 GB | Gemma 4 12B | `IQ4_XS` | off | f16 | 140K |
| 16 GB | Gemma 4 12B | `UD-Q5_K_XL` | q8_0 | f16 | 172K |
| 24 GB | Gemma 4 26B-A4B | `UD-Q4_K_XL` | q8_0 | f16 | 100K |
| 32 GB | Gemma 4 31B | `UD-Q4_K_XL` | q8_0 | f16 | 112K |
| 48 GB | Gemma 4 31B | `UD-Q8_K_XL` | q8_0 | f16 | 128K |

`python scripts/hardware.py --table gemma` prints the full per-quant
matrix. On the 5090, each of those numbers was checked by loading the
model and measuring:

| Build | Gen t/s, MTP on | Gen t/s, MTP off | MTP accept |
|---|---|---|---|
| 31B `UD-Q5_K_XL` | 145–148 | 58 | 63% |
| 12B `UD-Q8_K_XL` | 190–212 | 85 | 53–62% |
| E4B `UD-Q8_K_XL` | 273–279 | 166 | 43–45% |

Each figure is an 800-token answer to a coding prompt, with thinking on.
Tool calls with `tool_choice` set to `auto` and to `required` both came
back as proper tool calls.

**How it differs from Qwen3.8:**

- **Sliding-window attention instead of DeltaNet.** Gemma has five
  local layers (1,024-token window) for every global layer. Only the
  global layers grow with context, so the same sizing arithmetic
  applies. Each token costs more than the KV cache alone, though,
  because flash attention keeps an f16 copy of one global layer's K
  and V. kiln counts that, and the MTP head pays it again. Without it,
  the 31B `UD-Q5_K_XL` with MTP and vision would have been sized to
  108K on a 32 GB card and run out of memory. The numbers are in
  `scripts/hardware.py`, above the Gemma entries.
- **It thinks only when asked**, so `kiln start` passes
  `--reasoning on`. Qwen's template thinks by default. Either way the
  thinking lands in `reasoning_content`, not in your code.
- **Google's sampling**: temperature 1.0, top-p 0.95, top-k 64.
- **Draft length 4**, the setting unsloth measured, rather than Qwen's 3.
- **E4B keeps its per-layer embedding table in system RAM** (1.5–2.9 GB,
  depending on the quant), so it needs that much less VRAM than its
  file size suggests.

---

## Choosing a quant

| Quant | Size | Est. KLD vs BF16 | Notes |
|---|---|---|---|
| `Q8_0` | 29.0 GB | ~0.01% | reference for benchmarking only |
| `UD-Q6_K_XL` | 25.9 GB | ~0.05% | needs 48 GB to keep the MTP head |
| **`UD-Q5_K_XL`** | **20.2 GB** | **~0.2%** | **default — best quality that keeps MTP on 32 GB** |
| `UD-Q4_K_XL` | 17.6 GB | ~0.7% | 192K on 32 GB with MTP |
| `UD-IQ4_XS` | 14.3 GB | ~1% | **24 GB pick** — keeps MTP with the q4_0 head |
| `UD-Q3_K_XL` | 13.1 GB | ~2% | more window on 24 GB, weaker tool-calling |
| `UD-IQ3_XXS` | 10.9 GB | ~3% | below the floor `kiln` will pick on its own |

Eleven quants are available for both 27B builds; the full ladder is in
`scripts/hardware.py`, and `kiln get` lists every one with the context it
would leave on your card. `kiln` will not choose below `UD-IQ4_XS` by
itself — see the note about the quality floor above.

The KLD column is a **general pattern for 27B-class dense models, not a
measurement of Qwen3.8**. To measure it on your own files, run
`kiln quality` (`./kiln.sh quality` on Unix) — it scores each quant you have
against Q8_0 by KL-divergence.

Why KL-divergence rather than perplexity: for agentic coding the failure
mode is not worse prose, it is **malformed tool-call JSON and dropped
instructions on step 12 of a 20-step task**. Perplexity can look fine
while the argmax token flips; KLD and the "same top token" rate catch
that.

### VRAM budget at the default (UD-Q5_K_XL, 112K ctx, 32 GB card)

```
weights  UD-Q5_K_XL        18.83 GiB
MTP head Q8_0               2.95 GiB
mmproj   F16                0.86 GiB
KV @112K q8_0 (16 layers)   3.72 GiB
MTP draft KV                0.50 GiB
DeltaNet recurrent state    0.15 GiB
compute buffer             ~2.00 GiB
─────────────────────────────────────
total                     ~29.5 GiB   of ~30.3 GiB usable
```

---

## Using with Cursor

**One-time setup** — Settings (`Ctrl+Shift+J`) → Models:

1. **Base URL** — check *Override OpenAI Base URL*, paste the URL the
   start script prints, with `/v1` appended.
2. **API key** — paste the key the start script prints (from
   `api_key.txt`). Requests without it are rejected.
3. **Add model** — type `qwen3.8-27b` in *Add Model*, press Enter, click
   **Verify**. If you also run the abliterated build, add
   `qwen3.8-27b-abliterated` as a second model; the launcher prints which
   name the running server answers to.
4. Uncheck provider models you don't want to hit by accident.

**Every session:** the quick-tunnel URL changes on restart, so update the
Base URL each time. To stop that, see below.

| Cursor feature | Works? |
|---|---|
| Chat (`Ctrl+L`) | Yes |
| Inline edit (`Ctrl+K`) | Yes |
| Composer / multi-file | Yes |
| `@Codebase` | Yes |
| Tab autocomplete | No — requires a Cursor-hosted model |

### Getting a URL that does not change

The quick tunnel is zero-setup but rotates its hostname on every restart.
For a stable address, register a
[named Cloudflare tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):
you get a fixed subdomain on a domain you control, and can put a
Cloudflare Access policy in front of it instead of relying on the API key
alone. Worth the fifteen minutes if you use this daily.

---

## Using with OpenCode

```bat
kiln opencode          :: Windows
```
```bash
./kiln.sh opencode     # WSL, Linux, macOS
```

This adds a `kiln` provider to OpenCode's global config
(`~/.config/opencode/`, which is `C:\Users\<you>\.config\opencode` on
Windows) and leaves everything else in that file alone. **Restart
OpenCode afterwards** — it reads its config only at startup, and the
desktop app keeps running in the tray after its window closes. Then pick
a model with `/models`, or run `opencode -m kiln/qwen3.8-27b`.

- **Every build you have downloaded is listed**, each with the context
  window `kiln start` would open for it on your card, so OpenCode
  compacts before llama-server runs out of room. Only the build that is
  running answers; switching builds needs no re-run.
- **The API key is referenced, not copied.** The config points at
  `api_key.txt`, so `kiln key rotate` takes effect in OpenCode on its
  next start. If you move or delete the repo, OpenCode refuses to load
  its config until you re-run `kiln opencode`, or undo it first with
  `kiln opencode --remove`.
- **Re-run it after downloading another build.** It replaces its own
  entry and nothing else. The first time it edits an existing file it
  saves the original, comments included, as `*.kiln.bak`.
- **It refuses rather than guesses.** A config it cannot parse, a
  `provider` section that is not an object, or a missing `api_key.txt`
  stops it with an explanation and the file untouched.

**WSL counts as a separate machine.** OpenCode in WSL and OpenCode on
Windows keep separate configs, so run it on each side you use. It works
out the address for you:

| Server runs in | OpenCode runs in | URL written |
|---|---|---|
| Windows | Windows | `127.0.0.1` |
| WSL or Linux | the same place | `127.0.0.1` |
| WSL | Windows | `127.0.0.1` (WSL forwards it) |
| Windows | WSL, mirrored networking | `127.0.0.1` |
| Windows | WSL, NAT networking (the default) | the Windows host IP |

The last row is the fragile one: that IP changes whenever WSL restarts,
and Windows Firewall may block it. Setting `networkingMode=mirrored`
under `[wsl2]` in `%USERPROFILE%\.wslconfig`, then running
`wsl --shutdown` and re-running `./kiln.sh opencode`, moves you to
`127.0.0.1` for good.

It only trusts what answers like llama-server, so another service on
port 8080 is not mistaken for yours. One case it works around: WSL
forwards anything published on 8080 inside WSL (a Docker container, say)
to Windows' `127.0.0.1:8080`, which then shadows llama-server for every
Windows client. OpenCode is pointed at the machine name instead, but the
Cloudflare tunnel still forwards to `localhost` and breaks, so move the
other service off 8080.

To point at a server on another machine or behind a named tunnel, pin
the address with `kiln opencode --url https://llm.example.com/v1`, or use
`--print` to see the block without writing it.

---

## Using with pi

[pi](https://pi.dev) is a small terminal coding agent. Install it with
`npm install -g --ignore-scripts @earendil-works/pi-coding-agent`, which
needs Node 22.19 or newer. The older `@mariozechner/pi-coding-agent`
package is deprecated. Then:

```bat
kiln pi                :: Windows
```
```bash
./kiln.sh pi           # WSL, Linux, macOS
```

This adds a `kiln` provider to pi's `models.json` (`~/.pi/agent/`, which
is `C:\Users\<you>\.pi\agent` on Windows, or `$PI_CODING_AGENT_DIR`) and
leaves everything else in that file alone. pi reads the file again
whenever `/model` opens, so a running pi needs no restart. Pick a model
there, or run `pi --model kiln/qwen3.8-27b`.

- **Use this rather than pi's built-in llama.cpp provider.** That one
  drives llama-server's router mode. `kiln start` serves a single model
  with its MTP draft head, so it would list nothing. The `kiln` provider
  sends llama-server the same settings pi's own provider does.
- **Every build you have downloaded is listed**, with the context window
  `kiln start` would open for it, the same as for OpenCode.
- **Thinking is on or off.** `/thinking` offers `off` and `medium`,
  which turn Qwen's thinking off and on through its chat template.
- **The API key is referenced, not copied.** pi runs `cat` on
  `api_key.txt` for every request it sends to a kiln model, so `kiln key
  rotate` takes effect at once, with no re-run. On Windows pi runs that
  command through Git Bash, which it needs anyway. `kiln pi` says so
  when pi would fall back to WSL's bash or cmd, which cannot run it.
- **It refuses rather than guesses**, as `kiln opencode` does. pi
  rejects all of `models.json` when any part of it is invalid, which
  would hide your other providers too, so CI checks every write against
  pi's schema.

pi in WSL and pi on Windows are separate installs, so run `kiln pi` on
each side you use. The address is worked out as in the
[OpenCode table](#using-with-opencode), and `--url`, `--print` and
`--remove` work the same way.

---

## Security

The quick-tunnel URL is **public** — anyone who guesses or scrapes the
hostname can reach it. Every start script therefore generates a random
key into `api_key.txt` and passes it via `--api-key-file`. Requests
without it are rejected. Cursor makes you fill in an API-key field
anyway, so this costs nothing.

The key is 24 bytes from the platform CSPRNG, and the file is locked to
your account on both platforms — `chmod 600` on Unix, and on Windows
`icacls /inheritance:r /grant:r` so other accounts on the machine cannot
read it. That lockdown is reapplied on every launch, so a key written
before this existed gets fixed the next time you start the server.

```bat
kiln key show           :: print it (also generates it the first time)
kiln key rotate         :: throw it away and make a new one
kiln key set MY-SECRET  :: use a passphrase you choose instead
```

`kiln key set` accepts anything on one line — llama-server compares it
verbatim. Rotating or setting takes effect on the next server start, and
you have to paste the new value into Cursor.

The server also binds `0.0.0.0`, so the key is what protects you on any
shared network, not just over the tunnel.

---

## Scripts

You only ever type `kiln` (or `./kiln.sh`). Everything below sits in
`scripts/` and is dispatched for you — this table is a map for people
who want to read or change it.

```
kiln.bat / kiln.sh     the front door you type
scripts/windows/       everything cmd.exe runs
scripts/unix/          everything macOS and Linux run
assets/banner.txt      the wordmark
NOTES.md               why the tuned constants are what they are
```

**The CLI**

| Script | Purpose |
|---|---|
| `kiln.bat` | Windows front door. A shim onto `scripts/windows/kiln.bat`. |
| `kiln.sh` | macOS/Linux front door. Detects the platform and dispatches to `scripts/unix/`. |
| `scripts/windows/kiln.bat` | The real CLI. No arguments open the TUI; `status` prints the board. |

**Windows** (`scripts/windows/`)

| Script | Purpose |
|---|---|
| `install.bat` | One-time setup: llama.cpp binaries, cloudflared, `models/`. Takes the newest CUDA build of the newest bNNNN release, with its matching cudart. |
| `update.bat` | Upgrade llama.cpp to the latest release and its matching cudart, backing up the old build. Verifies MTP support afterward. |
| `download.bat` | Weights + MTP head + vision projector, for one build. Resumable. |
| `launch.bat` | Model picker: every build, Qwen and Gemma, downloading first if needed. Takes a variant name (`base`, `gemma-26b`, ...) to skip the menu. |
| `start.bat` | Tuned launcher. Auto-detects build, quant, MTP, vision, VRAM and cores. |
| `probe_hardware.ps1` | Fallback sizer for machines without Python. `hardware.py` is the normal path. |
| `benchmark.bat` | Quant speed, ubatch sweep, KV precision, throughput vs depth. |
| `quality.bat` | KL-divergence of each quant against Q8_0. |
| `cleanup.bat` | Reclaim space from superseded GGUFs. Requires typing `DELETE`. |
| `lib_variants.bat` | Resolves a build to its repo, filename prefix, Cursor alias, MTP head, projector and launch flags. Reads the registry from `hardware.py`. |
| `lib_api_key.bat` | Generates the API key, locks the file to your account, hands back `API_KEY`. |
| `lib_ui.bat` | ANSI palette for the CLI. Degrades to empty strings under `NO_COLOR`. |
| `kiln_path.ps1` | Puts `kiln` on PATH: a `kiln.cmd` shim in `%USERPROFILE%\.local\bin`, and that folder on the user PATH. Behind `kiln path` and `kiln setup`. |

**Shared** (`scripts/`)

| Script | Purpose |
|---|---|
| `hardware.py` | The model registry and the sizing arithmetic. Detects the GPU, picks model + quant + MTP + context, and generates the tables above. Used by every other script on both platforms. |
| `opencode.py` | Writes the `kiln` provider into OpenCode's config. Detects Windows, WSL or Linux and works out the server URL. Behind `kiln opencode`. |
| `pi_agent.py` | Writes the `kiln` provider into pi's `models.json`, with the same server probe and context sizing as `opencode.py`. Behind `kiln pi`. |
| `selfupdate.py` | Fast-forwards the checkout to the newest release, or to `main` if you opted in. Refuses rather than overwrite local edits or commits. Behind `kiln self-update` and the board's `kiln` row. |
| `control.py` | Reads the same state as the status board and runs kiln commands for `kiln tui` and the benchmark, from a fixed list. |
| `bench.py` | The benchmark: starts each setup with `kiln start`, times real requests, recommends one. Also runs on its own. |
| `tui.py` / `tui_app.py` | `kiln tui`. `tui.py` installs Textual into `.kiln/` on first run, and `tui_app.py` is the app. |

**macOS and Linux** (`scripts/unix/`)

| Script | Purpose |
|---|---|
| `install_mac.sh` / `install_linux.sh` / `install_fedora.sh` | One-time setup, per platform. |
| `kiln_path.sh` | Links `~/.local/bin/kiln` to this checkout's `kiln.sh`. Behind `kiln path` and the setup scripts. |
| `download.sh` | Weights + MTP head + vision projector. `QUANT=` and `VARIANT=` override. |
| `start_mac.sh` / `start_linux.sh` / `start_fedora.sh` | Tuned launchers. Context comes from `hardware.py`, falling back to `--fit on`. |
| `slurm-llama.sh` | Run the server as a Slurm job so the GPU shows as allocated. |
| `start-cloudflared-once.sh` | Bring up the tunnel on its own. |
| `benchmark.sh` / `quality.sh` / `cleanup.sh` | Shell twins of the Windows tools. |
| `lib_variants.sh` / `lib_api_key.sh` | Shell twins of the Windows libraries. |
| `lib_select.sh` | Picks the weights, MTP head, vision projector and context for a launch, and builds the per-family server flags. Shared by all three start scripts and the Slurm job. |
| `lib_python.sh` | Finds a Python that actually runs, and strips the CR that Windows Python puts on every line. |

---

## Tuned server settings

| Flag | Value | Why |
|---|---|---|
| `--n-gpu-layers` | 99 | all 64 layers on GPU |
| `--ctx-size` | detected | computed from your VRAM by `scripts/hardware.py`, on both platforms; falls back to `--fit on` when there is no Python or no GPU to read |
| `--flash-attn` | `auto` | not `on` — gated attention at 256 head-dim falls back cleanly instead of erroring |
| `--cache-type-k/v` | `q8_0` | halves KV vs f16; **not** `q4_0`, see the recurrent-layer note above |
| `--parallel` | `1` | **the easy win.** The default (`-1` = auto) splits the KV cache across slots, so each request gets `ctx/N`. Single-user Cursor wants the whole window in one slot. |
| `--batch-size` / `--ubatch-size` | 4096 / 1024 | larger prefill batches; a 32 GB card has the bandwidth and headroom |
| `--threads` | detected | physical cores only — with everything on GPU, counting SMT siblings just adds contention |
| `--jinja` | on | correct tool-call parsing for agentic use |
| `--reasoning-format` | `deepseek` | puts chain-of-thought in `message.reasoning_content` so thinking tokens stay out of code output |
| sampling | `temp 1.0, top-p 0.95, top-k 20, min-p 0.0` | Qwen's published thinking-mode values |
| `--api-key-file` | `api_key.txt` | see Security |

The batch sizes and KV precision are reasoned from the architecture,
**not measured**. `kiln bench` (`benchmark.{bat,sh}`) measures them so
you can confirm or override.

---

## Troubleshooting

**Model loads but generation is slow.** Check that MTP actually engaged —
`server.log` should mention the draft model. If `--spec-type draft-mtp`
was rejected, your llama.cpp predates b9180; run `kiln update`.

**Out of memory at load.** The launcher sizes context to your card, but
its estimate is not exact. Drop to the next quant down, or delete the MTP
GGUF to free ~3.5 GB. See the GPU table above.

**"these weights do not leave room for any context".** The fixed costs
alone exceed your VRAM. Take a smaller quant.

**Sudden catastrophic slowdown on Windows.** When VRAM is exceeded, the
NVIDIA driver silently spills to system RAM instead of failing. Set
*NVIDIA Control Panel → Manage 3D Settings → CUDA - Sysmem Fallback
Policy* to **Prefer No Sysmem Fallback** for `llama-server.exe` so you get
a clean OOM instead of a 50× slowdown.

**Cursor says the model is unreachable.** The tunnel URL rotates on every
restart — re-paste it. If it still fails, confirm the API key in Cursor
matches `api_key.txt`.

---

## FAQ

**Can I use a local LLM with Cursor?** Yes. Cursor lets you override the
OpenAI base URL and add a custom model name, which is all an
OpenAI-compatible server needs. That is what this repo sets up.

**Do I still need a Cursor subscription?** You still need a Cursor
account, and some Cursor features are gated by your plan no matter which
model serves the tokens. But chat, inline edit, Composer and `@Codebase`
run against your own GPU here, so they do not consume request quota.

**Does my code leave my machine?** The model runs locally, so prompts and
completions stay on your GPU. Cursor itself still talks to Cursor's
servers for editor features and, if you use the quick tunnel, your traffic
is proxied through Cloudflare. For a fully local path, skip the tunnel —
see the next question.

**Can I run it without a tunnel?** The scripts support it: if `cloudflared`
is not installed they serve on `http://localhost:8080/v1` and say so.
Cursor's *Verify* step generally wants a URL it can reach from outside,
which is why the tunnel exists — but any other OpenAI-compatible client
works against localhost directly.

**Does Tab autocomplete work?** No. Cursor's Tab model is hosted by Cursor
and cannot be pointed at a custom endpoint. Chat, `Ctrl+K`, Composer and
`@Codebase` all work.

**Will it run on a 3090, 4090 or 7900 XTX (24 GB)?** Yes. `kiln` picks
`UD-IQ4_XS` with the smaller q4_0 MTP head, which keeps speculative
decoding at about 104K context — you do not have to choose between them.
Run `kiln hardware` to see it for your card.

**Will it run on 16 GB, or 12 GB?** Yes, but not the 27B — `kiln` moves
you to one of the smaller distills instead. See
[the tables above](#will-this-run-on-my-gpu).

**Why did `kiln` pick a different quant than the README's default?**
Because the default is for a 32 GB card and yours is not one. What `kiln
hardware` prints wins over any number written here; the tables are that
same calculation run ahead of time.

**Does this work with VS Code, Continue, Cline, Roo Code, Zed or Aider?**
Yes. The server is a plain OpenAI-compatible endpoint, so anything that
accepts a base URL, an API key and a model name will talk to it. Only the
Cursor-specific setup steps differ. OpenCode gets a one-command setup:
see [Using with OpenCode](#using-with-opencode).

**How fast is it?** That depends on your card, quant and draft acceptance
rate, so this repo ships a measurement path rather than a number. The
benchmark in `kiln tui` (press `b`) measures each setup as `kiln start`
runs it, MTP included. MTP speculative decoding is the single largest
lever — confirm it engaged before comparing anything.

**Why Qwen3.8-27B and not a bigger model?** 27B at `UD-Q5_K_XL` is the
largest capable coding model that fits a 32 GB card *while keeping the MTP
head resident*. See [what makes it
different](#what-makes-qwen38-27b-different).

**Is the abliterated build safe to expose?** It has had its refusal
behaviour removed and no published evaluation of its behaviour. If you
tunnel it, your API key is the only control in front of it. Read
[SECURITY.md](SECURITY.md) first.

---

## Contributing

Hardware reports are the most useful contribution — the GPU table is
computed, and only the RTX 5090 row is hand-validated. If you ran this on
a different card, [tell us what
loaded](../../issues/new?template=hardware_report.yml).

See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions, and
[NOTES.md](NOTES.md) for the reasoning behind the tuned constants and a
list of cmd.exe traps already found the hard way.

---

## Requirements

**Windows:** Windows 10/11, NVIDIA GPU with a CUDA 13 driver, Python 3.8+.
**macOS:** macOS 12+, Apple Silicon or Intel (Metal), Python 3.8+, `curl`, `unzip`.
**Linux:** NVIDIA driver with `nvidia-smi`, Python 3.8+, `curl`, `unzip`.

Disk: 20–30 GB per quant, plus ~4 GB for the MTP head and vision
projector.

---

## License

MIT — see [LICENSE](LICENSE).

Model weights are governed by their own licenses:
[Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B).
