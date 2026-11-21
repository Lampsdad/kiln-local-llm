# Maintainer notes

Machine-specific context that used to live in the README. None of this is
needed to use the repo — it is here so the tuning decisions stay
reproducible.

## Reference machine

The defaults in this repo were developed and validated on:

| | |
|---|---|
| GPU | RTX 5090, 32,607 MiB VRAM, Blackwell sm_120 |
| CPU | Ryzen 9 9900X3D, 12C/24T (split CCDs) |
| RAM | 61.6 GB |
| Disk | single ~1.86 TB volume |
| OS | Windows 11 Home |

`scripts/windows/start.bat` no longer hardcodes any of this — it reads
VRAM and physical core count via `probe_hardware.ps1` at launch. The numbers
above are what the hand-loaded 131K context figure was measured against
(see the formula note below — the probe computes 112K / 114688).

## Context-sizing formula

`probe_hardware.ps1` computes:

```
free = VRAM - 1536 (driver) - weights - MTP - mmproj
            - 2048 (compute buffer) - 512 (draft KV) - 160 (DeltaNet state)

ctx  = floor( free * 0.85 * 1 MiB / 34,816 bytes-per-token / 4096 ) * 4096
```

The `0.85` covers allocation slack: buffers fragment, the compute buffer
grows with batch shape, and the driver footprint moves. Aiming at 100% of
the arithmetic budget reliably OOMs.

On the reference machine with `UD-Q5_K_XL` + MTP + vision this yields
**114,688** (112K), which is what the README tables report. **131,072**
is what actually loaded when tried by hand before the formula existed —
i.e. the 0.85 margin is real headroom, not a fit to 131K. Do not claim
the formula produces 131K; if you change a constant, re-check that the
probe still lands on 114688 for this card.

`34,816 bytes/token` is `16 cached layers × 4 KV heads × 256 head-dim × 2
(K+V) = 32,768 elements`, at q8_0's 34 bytes per 32-element block.

## Model variants

`MODELS` in `scripts/hardware.py` is the only place that knows what a
build *is*: repo, filename prefix, Cursor alias, quant ladder, MTP head,
vision projector, sampling, draft length and minimum llama.cpp build.
`lib_variants.bat` / `lib_variants.sh` read it and hand the fields to the
download, start and launch scripts, which are otherwise variant-blind.
Their hardcoded base/ablit copy is only the no-Python fallback.

**Why huihui-ai for the abliterated build.** There are on the order of
thirty abliterated Qwen3.8-27B GGUF repos. This one was picked because
its `UD-*` files are re-ablations of
`unsloth/Qwen3.8-27B-GGUF` — the exact quants this repo already targeted.
That makes the two builds directly comparable: same quant names, sizes
within 3%, so `probe_hardware.ps1` and the README context table need no
per-variant special-casing.

**The shared MTP head is an inherited claim, not a measurement.** Both
builds load `ggml-org`'s `mtp-Qwen3.8-27B-Q8_0.gguf` and unsloth's
`mmproj-F16.gguf`, on the strength of huihui's model card saying "MTP and
visual has not been modified". It is very likely fine — abliteration
changes weight values, not tensor shapes, and speculative decoding is
lossless regardless of how good the draft is, since the abliterated model
is still the verifier. The realistic failure mode is a *lower draft
acceptance rate*, showing up as reduced tok/s rather than wrong output.
If the abliterated build is unexpectedly slow, benchmark it with and
without the `--spec-type` block before looking anywhere else.

**Ablation depth.** huihui's `UD-*` series ablates layers 18–51 and
leaves 0–17 untouched, which is what keeps coding and tool-calling near
stock. They also publish a `UD-DW-*` series ablating only 23–51 (less
thorough, warns more); it is not wired up here.

**The byte sizes** in `scripts/hardware.py` are the actual sizes from
the Hugging Face file listings, read 2026-09-04 for Qwen, 2026-10-14
for Gemma 4 and 2026-11-20 for Inkling 2 Mini. They feed the download menu, the free-space check and every
context number. The stock Qwen column was once off by up to 1 GB in both
directions, so re-check if a publisher reuploads.

## Gemma 4

**Why opt-in.** Every number in this repo was tuned on Qwen3.8-27B, and
nothing here measures Gemma's coding quality against it. So Gemma is in
no `FAMILY_ORDER`, and kiln never picks it for you. `SIBLINGS["gemma"]`
is what a Gemma user gets pointed at when their model does not fit.
E4B is left out of it on purpose (Tau2 42% against the 31B's 77%).

**Why unsloth.** Its repos ship the UD quants, the vision projector and
the MTP drafter side by side, and the projector is the only file whose
name clashes. unsloth calls both the Qwen and the Gemma projector
`mmproj-F16.gguf`, so Gemma's is saved as `mmproj-<stem>-F16.gguf`. The
download scripts check for a file by its local name, and before this
they would have skipped Gemma's projector as "already present". The
registry keeps `remote` and `local` names apart for this reason.
`test_hardware.py` fails if two families ever map different files to
one local name.

**No abliterated Gemma, yet.** huihui-ai publishes abliterated Gemma 4
GGUFs only of Google's QAT checkpoints. Nobody has re-ablated unsloth's
quants the way huihui did for Qwen, so the quant ladder and the shared
MTP head would not carry over.

**How the sizing was checked.** On the 5090, at llama.cpp b11624, each
model was loaded at two or more contexts, with and without the MTP
head and the projector, and the VRAM llama-server added over idle was
read from nvidia-smi:

| Load | 32K | 64K | 96K | 128K | 256K |
|---|---|---|---|---|---|
| 31B `UD-Q5_K_XL` | 24,107 | | 27,469 | | |
| 31B + MTP | 25,081 | | 29,072 | | |
| 12B `UD-Q8_K_XL` | | 14,750 | | 15,560 | 17,159 |
| 12B + MTP | | 15,546 | | | 18,716 |
| E4B `UD-Q8_K_XL` | 6,585 | | | 7,988 | |

(MiB.) Per token that is 53,792 B for the 31B, 12,792-12,960 for the
12B and 14,965 for the E4B. That is more than the q8_0 KV cache alone
(43,520 / 8,704 / 8,704). The difference fits one global layer's K and
V at f16 plus a 2-byte mask entry per ubatch row, to within 1%, so
that is what `kv_bytes` counts. The MTP head adds the same scratch
again per token (measured 4,059 B on the 12B and 10,064 B on the 31B,
against 4,096 and 10,240 modelled), plus a fixed 4-163 MiB.

The fixed costs went the other way: `COMPUTE_BUF_MIB` + `state_mib`
over-counts by about 1.4 GB on all three. That is margin, and it is
left alone. The cross-check was the 31B `UD-Q5_K_XL` with MTP and
vision at the 72K the formula allows. It loaded with the whole card at
31,162 of 32,607 MiB, desktop included. Before the scratch was counted,
the formula put that same setup at 108K, about 2 GB more than the card
has.

The 26B-A4B was not loaded. Its numbers come from the same formula.

**E4B's weights are not all on the GPU.** Its per-layer embedding table
(256 x 42 layers x the 262K vocab) is read lazily from system RAM. With
Q8 weights, the server took 6,585 MiB at 32K, against an 8,308 MiB
file. The table is stored at Q8_0, Q5_K or Q4_K depending on the quant
(read from the GGUF headers), and `host_bytes` subtracts it.

**Minimum build.** Gemma MTP landed in b9549 (PR #23398). The drafter
fix (#28183, b10742), the vision fix (#28335, b10798) and the
required-tool-call grammar fix (#29115, b11059) came later. The
`draft-mtp` probe in the start scripts passes on all of those builds, so
Gemma carries its own `min_build`, and start refuses anything older.

## Inkling 2 Mini

**Why opt-in.** The same reason as Gemma: nothing here has measured its
coding quality against Qwen3.8-27B in Cursor. Its model card scores it
at or a little above Qwen3.8-27B on most coding benchmarks, but those
are its maker's numbers.

**Why ggml-org and not unsloth.** unsloth's UD quants were published
before upstream llama.cpp could load the model, for unsloth's own fork.
Their headers say `general.architecture = inkling2`. Upstream merged a
different layout, `inkling2_loop`, in #32694 (2026-11-20, b12600), and
that is what ggml-org's files use. unsloth also has upstream-layout
copies of its quants under `Shard_Rewrite/`. They are marked not for
general use, and they need a loop-pass fix that stock #32694 lacks. kiln
installs only upstream builds, so ggml-org's `Q8_0` and `IQ4_NL` are
what it can serve. Revisit this when unsloth moves its root files to
the upstream layout. Their UD ladder is finer, and their DSpark drafter
might then work as a draft head.

**Sizing.** The model is looped: layers 6–17 run three times per token,
and each pass keeps its own KV cache. With five sliding-window layers
for every global one, 8 of the 48 layer passes are global, so the KV
cache is only 17,408 B a token. That alone would leave room for about
170K on a 24 GB card. The compute buffer is what takes it away. Inkling has no
RoPE, and llama.cpp materialises its relative-position bias for each
global layer (`kq_b` in `src/models/inkling2.cpp`): n_kv x ubatch x 24
heads, f32, gathered and then copied. That is 198 B per context token
per ubatch row, so the compute buffer grows with context times ubatch.

It was found on the CPU build of the merge commit, before any CUDA
release existed. A 256K load at the default ubatch of 512 reserved a
25.5 GB compute buffer, against 4.5 GB of KV cache. On the 5090 at
b12600 (CUDA 12.8), IQ4_NL with no projector, MiB:

| Context | ubatch | KV + SWA | Compute | nvidia-smi delta |
|---|---|---|---|---|
| 32K | 128 | 657 | 838 | 18,428 |
| 64K | 128 | 1,201 | 1,630 | 20,064 |
| 128K | 128 | 2,289 | 3,214 | 22,176 |
| 32K | 256 | 657 | 1,678 | 19,405 |
| 64K | 256 | 1,201 | 3,262 | 21,360 |
| 64K | 512 | 1,222 | 6,524 | 24,623 |

The registry models it as `ubatch_ctx_bytes = 200` on top of
`kv_bytes`, and pins the family's `ubatch` so the start scripts launch
at the ubatch it was sized for. `start.bat` passes 1024 to everything
else, which for Inkling would be 200 KB a token. 256 was picked by
prefill speed against the 96K bar on 32 GB: 512 leaves 80K, 384 leaves
104K and 256 leaves 144K. Prefill of a 32K prompt runs 1,027 / 1,178 /
1,272 t/s at 256 / 384 / 512. The cross-check was `kiln start` at the
144K the formula allows, with the BF16 projector. The whole card sat at
29,367 of 32,607 MiB, desktop included, and 29,895 after a 123K-token
prompt. Only IQ4_NL was loaded. The Q8_0 numbers are the same
arithmetic.

Gemma's flash-attention scratch (one global layer's K and V at f16) is
not counted, because none showed up. Fitting the grid to compute =
a + b x ubatch per token gives b = 198 and a = 0. If upstream ever stops
materialising the bias, re-measure: `ubatch_ctx_bytes` would then be far
too high, and a scratch term may appear.

**Minimum build.** #32694 merged as b12600, released 2026-11-20. The
Inkling chat parser (#32381) is older. `--reasoning-effort`, which kiln
uses to start Inkling at `medium`, is in b12600 too.

**Why it is not faster than Qwen3.8-27B.** The card says 3.4B active,
and 5.4B counting the looped passes. Summing the GGUF's tensors, with
experts at 8 of 128 and layers 6–17 three times, a token reads 4.1 GB,
against 20.9 GB for Qwen's `UD-Q5_K_XL`. On bandwidth alone it would be
about five times faster. It is not: 77 t/s reads 334 GB/s of the
5090's 1,792, where Qwen's 65 t/s reads 1,382. The GPU drew about 250 W
while Inkling generated. A step costs a fixed ~13 ms, whatever it
reads: `llama-bench` batches of 1, 4 and 8 tokens took 13.3, 19 and
22 ms, against Qwen's 15.4, 16.6 and 16.9. The likely cause is the
graph: 12,402 nodes a token to Qwen's 3,993, most of them small. That
is llama.cpp's first-day implementation, and the model card warns that
local runtimes are new to looped models. Re-measure on a later build.
All on b12600 with `-fa 1 -ctk q8_0 -ctv q8_0`, 2026-11-21:

| | Inkling `IQ4_NL`, `-ub 256` | Qwen `UD-Q5_K_XL`, `-ub 1024` |
|---|---|---|
| tg128 | 77 | 65 |
| tg128 at 32K / 96K deep | 77 / 60 | 59 / 49 |
| pp4096 | 1,437 | 3,508 |
| pp4096 at 32K deep | 855 | 3,072 |
| as served (Qwen with MTP) | 69–76 | 98–134 |

Same prompts through llama-server with kiln's flags, default thinking:
an ISO 8601 duration parser took Inkling 123–150 s (7,800–9,100
thinking tokens), and Qwen 180 s (18,500, after two runs that hit a
16,000-token cap). A short explanation took 10–11 s against 15–17 s.
A question about a 66 KB pair of files took 34 s against 18 s, most of
it prefill. Inkling's tokenizer uses 8–15% more tokens for the same
text (3.09 characters a token on these answers, Qwen 3.34).

**Reasoning effort.** The template maps `none` … `max` to a "Thinking
effort level" line, and both a request's `reasoning_effort` field and
`chat_template_kwargs` reach it (checked with `/apply-template`), as
would a server-side `--chat-template-kwargs` default. On the parser
prompt, `medium` thought 1,700–2,200 tokens (40–50 s) and `low`
630–930 (22–33 s). Against 24 shared checks, both `high` answers passed
everything, one of two `medium` and neither `low` (both accepted
`P3DT`). kiln starts Inkling at `medium` (`--reasoning-effort`): a
third of the wait, at a cost that two answers a level can only hint
at. A request that sends `reasoning_effort` gets its own level, and
the server flag renders 0.7 when it sends none (checked).

**N-gram speculation.** With no draft head, `--spec-type ngram-mod`
drafts from matching text earlier in the context. Because a step's
cost barely grows with its batch, accepted drafts are close to free.
On "rename this parameter and add docstrings, return the code", the
rewrite ran at 60–155 t/s (about 100–110 on average) against 74, with
67% of drafted tokens accepted. On new code, 18% were accepted and the
run was about 5% slower. Prose, arithmetic and the long-file question
drafted nothing and gave byte-identical answers with the same seed.
It worked with the vision projector loaded.

**CUDA build versus driver.** b12600's CUDA 13.5 builds, Linux and
Windows, loaded and ran both models on a driver reporting CUDA 13.4
(616.64), at the 12.8 build's speed (Inkling 79 t/s, Qwen 65). That is
CUDA's minor version compatibility, so both installers now take the
newest build of the driver's major version rather than a named one.
By NVIDIA's rules a 12.x driver (before 580) cannot run a 13.x build at
all; that was not tested here. One consequence rests on the single
13.4 data point: a Linux driver at 13.0–13.3 now gets the 13.5 build,
where setup used to give it 12.8.

## cmd.exe traps found the hard way

An audit pass on 2026-09-01 turned these up. All were latent in shipped
scripts; the scanner for the first one is worth re-running after edits.

- **Unescaped parentheses in an `echo` inside a `( )` block.** The `)` in
  `echo ... (merged b9180).` closed the block early and cmd printed
  `. was unexpected at this time.` on every launch on a pre-b9180 build.
  Hit `start.bat`, `install.bat` and `cleanup.bat`. Escape as
  `^(` / `^)`.
- **`::` comments inside a `( )` block.** Labels are not valid there;
  use `rem`.
- **`for /f` splits backquoted commands on commas.** `nvidia-smi
  --query-gpu=name,memory.total` reaches the exe as two unknown options.
  Quote each option whole: `nvidia-smi "--query-gpu=name,memory.total"`.
  Only inside `for /f` — a direct invocation is fine.
- **`llama-server --version` prints `version: 8679 (...)`, not "build".**
  `findstr /C:"build"` matches the *`built with Clang`* line instead and
  yields the word `with`.
- **`timeout /t` refuses to run when stdin is redirected**, returning
  instantly with `ERROR: Input redirection is not supported`. Any wait
  loop built on it spins when the script is driven from another script or
  a pipe. `:sleep` in `start.bat` falls back to `ping`.
- **Bare `call foo.bat` depends on cmd searching the current directory**,
  which `NoDefaultCurrentDirectoryInExePath=1` disables. Use
  `call "%~dp0foo.bat"`.
- **`%~` inside a `::` comment is still expanded.** cmd substitutes
  parameters before it knows the line is a comment, so a comment that
  mentions `%~1` by name aborts the whole script with `The following
  usage of the path operator in batch-parameter substitution is
  invalid`. Describe it in words.
- **`if COND set A & set B` runs `set B` unconditionally.** This made the
  download script's free-space check always assume the largest quant.
- **A batch file rewritten while it runs resumes partway through a
  line.** cmd does not load a batch file into memory. It reopens the
  file for every line and continues from a saved byte offset. `kiln
  self-update` replaces both `kiln.bat` files while they are running,
  and in a test that grew the file, cmd went on to execute half a line
  of the new one. Whatever has to run after a step that rewrites the
  file goes on that step's line, for example `python ... & exit /b
  !errorlevel!`. A bare `exit /b` there keeps ERRORLEVEL for a caller,
  but `cmd /c` still exits 0, so the root shim uses `call exit /b
  %%errorlevel%%`.

## Disk

This machine runs a single ~1.86 TB volume, so it fills up. Two things
worth knowing:

- `~/.cache/huggingface` holds ~180 GB from *other* research projects.
  `cleanup.bat` deliberately does not touch it.
- The KL-divergence reference logits (`models/kld-base-qwen3.8.dat`)
  produced by `kiln quality` (`quality.{bat,sh}`) are large. Delete them once you
  have settled on a quant.

## Slurm

`scripts/unix/slurm-llama.sh` hardcodes `${HOME}/research/Local-LLM-setup-for-cursor`
as its working directory and requests `--gres=gpu:rtx5090:1` on the
`debug` partition. Both are site-specific; edit them for your cluster.

Do not start it while a manual `llama-server` is already running — they
will double-book the GPU.

## Superseded

Qwen3.6-35B-A3B was the previous default. It was replaced by Qwen3.8-27B
in August 2026: better coding scores, native MTP, and a hybrid-attention
stack that makes long context far cheaper. The Qwen3.6 scripts have been
removed; `cleanup_disk` will offer to delete the old weights.
