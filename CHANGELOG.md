# Changelog

Notable changes to the scripts. Dates are the commit dates on `main`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **`kiln pi`** (`./kiln.sh pi`) lists the local models in
  [pi](https://pi.dev), the terminal coding agent. It adds a `kiln`
  provider to pi's `models.json` and takes the same `--print`,
  `--remove` and `--url` as `kiln opencode`. pi's built-in llama.cpp
  provider needs llama-server's router mode, so it lists nothing for a
  server `kiln start` runs. The key is read from `api_key.txt` by a
  command rather than copied. `p` in `kiln tui` runs it.
- **Cursor follows the quick tunnel.** `scripts/unix/cursor_tunnel.sh`
  starts cloudflared and writes the new `*.trycloudflare.com` host into
  Cursor's OpenAI base URL, with the API-key workaround on. Stopping
  the tunnel turns that workaround off and clears a trycloudflare URL.
  Cursor's saved key is left as it is: the desktop encrypts it.
- **Gemma 4, opt-in.** `kiln get gemma-26b` and `kiln start gemma-26b`
  serve Google's Gemma 4. There are four variants: `gemma-31b`,
  `gemma-26b` (26B-A4B MoE), `gemma-12b` and `gemma-e4b`. Each one has
  its own MTP draft head and vision projector, and the same arithmetic
  as Qwen sizes the download and the context to your card. kiln never
  picks Gemma on its own. `kiln hardware`, a bare `kiln get` and a bare
  `kiln start` all stay on Qwen3.8, unless Gemma is the only thing
  downloaded. If a Gemma model is too big for the card, kiln points you
  at a smaller Gemma model. It never suggests E4B, which is weak at
  tool calls. Gemma needs llama.cpp b11059 or newer, and `kiln start`
  refuses an older build before it stops the running server.
  `hardware.py --table gemma` prints the Gemma context table. The sizing
  was checked by loading the 31B, 12B and E4B on a 5090. That showed
  Gemma's attention scratch and its MTP head both cost VRAM per token.
  Counting that keeps the 31B on a 32 GB card from running out of
  memory. The benchmark measures and picks within the model line you
  run, as it already did for abliterated builds, and it never picks
  E4B while anything else was measured.

### Changed

- **Each model family carries its own launch settings.** The registry in
  `scripts/hardware.py` now holds each family's MTP head and projector,
  sampling, draft length, reasoning flag and minimum llama.cpp build.
  The start scripts read these instead of assuming Qwen's. Qwen launches
  with exactly the flags it had before. The four Unix start scripts now
  share one copy of this logic in `lib_select.sh`.

- **Bare `kiln` opens the TUI.** `kiln` with no arguments (`./kiln.sh`
  on macOS and Linux) starts the full-screen app. `kiln status` still
  prints the status board. If the TUI cannot start — no Python, or the
  output is not a terminal — kiln says why and prints the board instead
  of hanging. Unix setup links `kiln` to `~/.local/bin/kiln` without
  sudo. That link resolves back to the checkout, so the command works
  from any directory.
- **`kiln` on PATH on Windows too.** `kiln setup` writes a `kiln.cmd`
  shim into `%USERPROFILE%\.local\bin` and adds that folder to the user
  PATH, so `kiln` runs from any folder in a new terminal. No admin
  rights are needed. The rest of the user PATH is kept as it was:
  `%USERPROFILE%`-style entries stay unexpanded. `kiln path` does only
  this step on every platform, and `kiln path --remove` undoes it. It
  never replaces or deletes a `kiln` that some other tool put there.
  On macOS and Linux, setup now says how to put `~/.local/bin` on PATH
  when it is missing, which it always is on macOS.

### Fixed

- **Fedora setup no longer downloads a Vulkan build from `/releases/latest`.**
  That URL is the empty v0.x line, so `./kiln.sh setup` on Fedora found
  nothing. An NVIDIA card now gets the Ubuntu CUDA build whose toolkit
  the driver can load (12.8 unless the driver reports 13.4 or newer),
  plus the separate cudart tarball. Fedora does not ship those libraries,
  and the server looks for them beside the binary. No NVIDIA card still
  falls through to Vulkan, then CPU. The WSL/Linux installer keeps
  Vulkan and uses the same bNNNN tag lookup.
- **Linux and WSL serve on port 8080 again.** `start_linux.sh` had
  moved to 8081, while `kiln opencode`, the Cursor tunnel and the docs
  all use 8080, so on Linux and WSL they pointed at nothing. The Slurm
  job keeps 8081.
- **Config backups keep the file's permissions.** The `.kiln.bak` that
  `kiln opencode` and `kiln pi` make, and the temporary file each write
  goes through, start with the mode of the file they copy, so a config
  readable only by you never sits in a readable copy.
- **`kiln stop` stops only kiln's tunnel.** It, the macOS and Fedora
  start scripts and the Slurm job ran `pkill -x cloudflared`, and on
  Windows `kiln stop` and `kiln start` ran `taskkill /IM cloudflared.exe`.
  That stopped every cloudflared on the machine: a named tunnel, a
  tunnel run as a Windows service, or T3 Code's remote access. kiln now
  stops only the quick tunnel to its own port, matched by its command
  line, through `scripts/unix/lib_tunnel.sh` and
  `scripts/windows/kiln_tunnel.ps1`. `kiln tui` uses the same rule
  before it shows a tunnel URL, so another program's tunnel no longer
  makes an old URL look live.
- **`./kiln.sh get` on Fedora and Debian.** A plain `pip install` into
  the system Python is refused there. The download uses `huggingface_hub`
  when it is already installed, and otherwise installs it for the user.
- **`kiln update` and `kiln setup` on Windows.** llama.cpp moved its
  Windows CUDA builds from 13.3 to 13.4 at b11000. Both scripts were
  pinned to 13.3, so `kiln update` failed with a 404. `kiln setup` also
  read `/releases/latest`, which has no Windows CUDA build at all. Both
  now take the newest bNNNN release. From it they take the newest CUDA
  build that has a cudart zip of the same version, and they download
  that zip too. `kiln setup` also failed to parse, with
  `... was unexpected at this time`, in two places. One was its download
  progress line. The other was the cloudflared block, which breaks on
  the `(x86)` in the install path even when cloudflared is already
  installed.
- **The Windows status board shows the llama.cpp build again.** Newer
  builds print `version: 0.6.0-dev (build 11624, ...)`, and the board
  showed `build 0.6.0-dev`.

## [1.1.0] — 2026-10-01

### Added

- **`kiln tui`** (`./kiln.sh tui`) is the status board and the everyday
  commands as a full-screen terminal app. You can start, switch and stop
  models, download one, copy the URL and API key Cursor needs, set up
  OpenCode, and update kiln, with each command's output shown as it
  runs. It runs the same kiln commands rather than reimplementing them.
  The first run offers to install Textual into `.kiln/`, a folder inside
  the checkout. It needs Python 3.9+. A model started from it keeps
  serving after you quit. It is also item 9 on the Windows menu.
- **A benchmark in `kiln tui`** (press `b`, or `scripts/bench.py`)
  starts each downloaded model with and without the MTP head through
  `kiln start`, with the tunnel off. It times prefill, generation, MTP
  acceptance and, on a thorough run, generation 32K tokens deep. Then it
  recommends the fastest setup that keeps 96K of context, and starts
  the one you pick. Setups whose runs overlap are called a tie, and the
  abliterated build is only measured and picked if it is the one you
  run. `p` copies the results as a markdown table for a hardware report.
  Unlike `kiln bench`, it measures what MTP is worth.
- **`KILN_NO_TUNNEL=1`** makes every start script skip the Cloudflare
  tunnel. `l` in `kiln tui` sets it for the next start.
- **`./kiln.sh start --no-mtp`** trades the MTP draft head for a larger
  context window on macOS and Linux, as `kiln start --no-mtp` already
  did on Windows.
- **A warning when another program holds the server's port.** On
  Windows a WSL or Docker container publishing 8080 takes
  `127.0.0.1:8080` ahead of llama-server, so localhost and the tunnel
  reach that program instead. `kiln tui` says so and finds the server by
  the machine's name.
- **`.github/scripts/test_control.py`, `test_bench.py` and
  `test_tui.py`** run in CI. They check that only the fixed command list
  reaches a command line, that a benchmark never opens a tunnel and
  always frees the GPU, and that the app's keys do what they say.
- **`kiln self-update`** (`./kiln.sh self-update`) updates kiln itself
  from GitHub. By default it moves to the newest `vX.Y.Z` release.
  `--main` follows every change on `main` instead and is remembered.
  `--check` only reports. The update is a git fast-forward, so `models/`,
  `llama-bin/` and `api_key.txt` are never touched. It refuses, changing
  nothing, when there are local edits, local commits or another branch
  checked out. It is also item 8 on the Windows menu.
- **A `kiln` row on the status board** shows the version you are on, and
  the newest release when there is a newer one. It asks GitHub at most
  once a day with `git ls-remote`. `KILN_NO_UPDATE_CHECK=1` turns that
  off.
- **`.github/scripts/test_selfupdate.py`** runs in CI against throwaway
  repositories and checks that every refusal leaves HEAD where it was.
- **`kiln opencode`** (`./kiln.sh opencode`) — lists the downloaded
  Qwen builds in OpenCode as `kiln/<alias>`, each with the context window
  `kiln start` would open for it. Works on Windows, WSL and Linux, and
  works out the WSL-to-Windows address itself. Merges into an existing
  config rather than replacing it, and references `api_key.txt` instead
  of copying the key. `--remove` undoes it. Refuses, leaving the file
  untouched, when the config cannot be parsed or the key file is missing.
- **A Python job in CI** that byte-compiles every script and runs
  `.github/scripts/test_opencode.py`.
- **`kiln hardware`** — reads the GPU and prints the model, quant, MTP
  head, vision projector and context window that fit it, which is exactly
  what `kiln get` will download. Available as `kiln hardware` and
  `./kiln.sh hardware`.
- **`scripts/hardware.py`** — the model registry and the sizing
  arithmetic, shared by every script on both platforms. Holds the repos,
  the quant ladders with exact byte counts, and the per-family KV-cache
  model. Also generates the context tables in `README.md`, so the docs
  cannot drift from the code.
- **Two smaller model families**, for cards a 27B has no business on:
  `9b` and `4b`, Empero's Apache-2.0 distillations of Qwen3.8-2.4T-A95B.
  Both are third-party rather than Qwen releases, and are labelled as such
  everywhere they are offered. Both are text-only — no MTP head, no vision.
- **Six more 27B quants.** The ladder is now the eleven `UD-*` names
  present in both the unsloth and huihui-ai repos, rather than five.
- **The q4_0 MTP head and the q8_0 vision projector.** 1.4 GB and 0.3 GB
  smaller than the ones used before, and the reason a 24 GB card can now
  keep speculative decoding.

### Changed

- **The default model and context window are chosen from your GPU, not
  assumed.** `kiln get` used to default to `UD-Q5_K_XL` on every machine —
  right on the 32 GB card this repo was tuned on, a guaranteed OOM on a
  12 GB one. Downloads, launches and menus now default to what fits, and
  say what they picked. An explicit `QUANT=` or `VARIANT=` still wins.
- **24 GB cards no longer have to choose between MTP and quant quality.**
  `UD-IQ4_XS` with the q4_0 head reaches ~104K with speculative decoding
  on. The README previously recommended giving up MTP for `UD-Q4_K_XL` at
  72K; that advice predated the smaller head.
- **`kiln start` prefers the tier's quant among the files on disk** and
  prints a one-line note when a better one exists, but never re-picks
  weights behind your back.
- The three Unix start scripts and `slurm-llama.sh` now share
  `lib_select.sh` instead of each carrying its own hardcoded quant, MTP
  filename and context.
- **The root `kiln.bat` exits on the same line that calls the CLI.** An
  update that rewrites the file can no longer leave cmd reading half a
  line of the new version.

### Fixed

- The `kiln setup` and `kiln update` lines of the README quick start had
  run together onto one line.
- **The documented 131K context on a 32 GB card was never what the code
  produced.** The probe computes 114688 with `UD-Q5_K_XL` + the q8_0 head
  + f16 vision; 131072 is what loaded when tried by hand. The comment
  claiming the 0.85 safety factor reproduced 131072 was wrong, and every
  131K in the README has been corrected to the computed figure.
- **`start_mac.sh` and `start_fedora.sh` exited silently when the vision
  projector was absent.** `[ -f "$MMPROJ" ] && EXTRA+=(...)` returns
  non-zero under `set -euo pipefail`, killing the script before it printed
  anything. Same bug in `slurm-llama.sh`.
- **The Unix scripts could not find Python on Windows shells.** `command
  -v python3` resolves to the Microsoft Store alias stub, which exits 0
  and prints nothing, so every lookup silently fell back. `lib_python.sh`
  now verifies the interpreter answers, and strips the CR that Windows
  Python puts on each line — a trailing `
` made `V_MTP` compare unequal
  to `1` while still printing as `1`.
- **The status board's "shared files" row** reported "no MTP head" on
  machines given the q4_0 head, because it matched the q8_0 filename only.

## [1.0.0] — 2026-09-03

First tagged release. The repo has been usable for months; this marks the
point where the Qwen3.8 path, the API key, and the `kiln` CLI are all in
place across every platform.

### Added

- **`kiln`, a single entry point on every platform.** Prints a status
  board of what is installed, downloaded and running, names the one thing
  to do next, and offers a menu. Verbs for every step: `status`, `setup`,
  `update`, `get`, `start`, `stop`, `key`, `bench`, `clean`. `kiln.bat` on
  Windows, `./kiln.sh` on macOS and Linux — the latter detects your
  distribution and dispatches to the right platform script.
- **A `scripts/` layout.** The repo root was 36 loose files; it is now the
  entry point plus documentation, with `scripts/windows/` and
  `scripts/unix/` holding the rest.
- **Shared libraries** — `lib_variants`, `lib_api_key`, `lib_ui` — so the
  Windows and shell script families no longer duplicate variant
  definitions or key handling.
- **Abliterated build** alongside the stock one, selectable at launch with
  `kiln start ablit` or `VARIANT=ablit`. Both builds share one copy of the
  MTP head and vision projector, so the second download is weights-only.
- **`scripts/unix/slurm-llama.sh`**, to run the server as a Slurm batch job so the GPU
  shows as allocated in `squeue`.
- **`kiln quality`**, scoring each quant against `Q8_0` by
  KL-divergence rather than perplexity.
- **`kiln bench`** — quant speed, ubatch sweep, KV precision and
  throughput vs. context depth.
- **`kiln clean`**, to reclaim space from superseded GGUFs. Requires
  typing `DELETE`.

### Changed

- **Default model is now Qwen3.8-27B**, replacing Qwen3.6-35B-A3B. Better
  coding scores, native MTP, and a hybrid-attention stack that makes long
  context far cheaper.
- **MTP speculative decoding is wired up by default** via
  `--spec-type draft-mtp`, which needs llama.cpp **b9180+**
  ([PR #22673](https://github.com/ggml-org/llama.cpp/pull/22673)).
- **Context is sized from your actual hardware.** `probe_hardware.ps1`
  reads VRAM and the weight files on disk and computes the window that
  fits; the shell scripts use llama.cpp's `--fit on`. No card is
  hardcoded any more.
- **KV cache precision raised to `q8_0`** from the `q4_0` used in the
  Qwen3.6 setup. In a 75%-linear-attention stack, low-bit KV error
  accumulates along the sequence instead of being re-anchored each token.
- **`--parallel 1`** rather than llama.cpp's auto default, which splits
  the KV cache across slots and hands a single-user Cursor session only
  `ctx/N` of its own context window.

### Removed

- **Legacy Windows forwarders** `run.bat`, `start_windows.bat` and
  `download_model.bat`. Use `kiln` instead.
- The `qwen` command is now `kiln`. It collided with Alibaba's official
  Qwen Code CLI and tied the tool to a single model family.

### Security

- **All platforms now generate an API key** into `api_key.txt` and serve
  with `--api-key-file`. The file is locked to the invoking account on
  every launch (`chmod 600`; `icacls /inheritance:r` on Windows). See
  [SECURITY.md](SECURITY.md).

### Fixed

- A batch of cmd.exe bugs that were latent in shipped scripts: unescaped
  parentheses inside `( )` blocks aborting the launcher, `::` comments
  inside blocks, `for /f` splitting `nvidia-smi` options on commas,
  `timeout /t` spinning when stdin is redirected, and
  `if COND set A & set B` running the second `set` unconditionally. All
  are written up in [NOTES.md](NOTES.md).
- Line endings pinned in `.gitattributes` — a `.sh` checked out with CRLF
  fails on Linux with `\r: command not found`.

## Earlier

- **2026-07-21** — Slurm launch path and a `cloudflared` helper.
- **2026-04-25** — Fedora support (thanks @EmeryJJ).
- **2026-04-24** — Initial local model setup, Windows start scripts, and
  Cursor instructions.

[1.1.0]: https://github.com/Lampsdad/kiln-local-llm/releases/tag/v1.1.0
[1.0.0]: https://github.com/Lampsdad/kiln-local-llm/releases/tag/v1.0.0
