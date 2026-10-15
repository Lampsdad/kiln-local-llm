# Contributing

Thanks for looking. This repo has a deliberately narrow goal: run
**Qwen3.8-27B well** on a single consumer GPU and serve it to Cursor. It
is not trying to become a general model launcher — Ollama and LM Studio
already do that. Contributions that make *this* model faster, or that make
the setup work on hardware it currently fails on, are the ones that fit.

## The most useful thing you can send

**A hardware report.** The context-window table in the README is
*computed* from the sizing formula in [`NOTES.md`](NOTES.md), and only the
RTX 5090 row has been validated by hand. If you ran this on a 3090, a
4090, a 7900 XTX or an Apple Silicon machine, open a
[hardware report](../../issues/new?template=hardware_report.yml) with the
window that actually loaded. That is how the table stops being an
estimate.

## Ground rules for tuned values

Several constants here are load-bearing and were derived from the
architecture rather than guessed:

- the `0.85` allocation-slack factor and `34,816` bytes/token in
  `scripts/windows/probe_hardware.ps1`
- `q8_0` KV cache precision (**not** `q4_0` — see the recurrent-layer note
  in the README)
- `--parallel 1`, and the 4096/1024 batch sizes

If you change one, say what you measured. `kiln bench`
(`benchmark.{bat,sh}`) and `kiln quality` (`quality.{bat,sh}`) exist so
that these can be checked rather than argued about. A PR that changes a number with a reason beats one that
changes it with a preference.

## Layout, and the one rule that matters

```
kiln.bat / kiln.sh     the front door
scripts/windows/       everything cmd.exe runs
scripts/unix/          everything macOS and Linux run
scripts/*.py           shared Python: hardware.py, opencode.py, ...
assets/                the wordmark
```

**Every script sets its working directory to the repo root, not to its
own directory.** `models/`, `llama-bin/`, `api_key.txt` and `server.log`
all live at the root and are referenced relatively, so a script that
skips this resolves them into `scripts/windows/` and silently does the
wrong thing.

```bat
cd /d "%~dp0..\.."
```

```bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."
```

Because the working directory is the repo root, a shell script must
source its libraries through `$SCRIPT_DIR`, not `./`:

```bash
. "$SCRIPT_DIR/lib_variants.sh"
```

Batch files call siblings with `%~dp0`, which is already relative to the
calling script, so those need no adjustment.

## Shell and batch conventions

Both script families are maintained in parallel — a change to
`scripts/unix/start_linux.sh` usually needs the matching change in
`scripts/windows/start.bat`.

- **Line endings are enforced by CI.** `.sh` must be LF, `.bat` and `.ps1`
  must be CRLF. `.gitattributes` pins this regardless of your
  `core.autocrlf`; do not fight it.
- **cmd.exe has traps** that have already bitten this repo — unescaped
  parentheses inside `( )` blocks, `::` comments in blocks, `for /f`
  splitting on commas, `timeout /t` failing under redirected stdin. They
  are all written up in [`NOTES.md`](NOTES.md). Read that section before
  editing a `.bat`.
- Shell scripts must pass `bash -n` and
  `shellcheck --severity=error`.
- Call sibling scripts as `call "%~dp0foo.bat"`, never bare `call foo.bat`.

## Adding a model variant

`scripts/windows/lib_variants.bat` and `scripts/unix/lib_variants.sh` are
the only files that know what a
build *is*. Everything else reads repo, filename prefix and Cursor alias
out of them and is otherwise variant-blind. A third variant should be a
branch there and nowhere else.

## What never gets committed

Weights, `llama-bin/`, logs, and `api_key.txt`. `.gitignore` covers them
and CI fails if any get tracked anyway.

## Cutting a release

`kiln self-update` moves people to the newest `vX.Y.Z` tag, not to the
tip of `main`. A merge to `main` reaches only the people who opted in
with `--main`. Everyone else gets it at the next tag:

1. In `CHANGELOG.md`, rename `[Unreleased]` to the new version and
   today's date, and start an empty `[Unreleased]` above it.
2. Commit that to `main` and wait for CI to pass.
3. Tag that commit and push the tag:

```bash
git tag -a v1.2.0 -m "v1.2.0"
git push origin v1.2.0
```

Publishing a GitHub release for the tag is optional. Self-update only
reads the tag.

- **Tag commits on `main`.** Users fast-forward their `main` to the
  tag, which only works if the tag is on its history.
- **Never move a tag once it is pushed.** Anyone who already updated to
  it is left on a commit the new tag does not descend from, and their
  next update refuses. Ship the fix as the next patch version instead.
- **Only exact `vMAJOR.MINOR.PATCH` names count.** `v1.2.0-rc1` or
  `test` is ignored, so a tag like that never reaches anyone.

Bump the patch number for fixes and the minor number for new commands
or changed defaults. Bump the major number when an update means
re-downloading weights or relearning commands.

## Running the checks locally

```bash
bash -n $(git ls-files '*.sh')
shellcheck --severity=error $(git ls-files '*.sh')
python3 .github/scripts/test_opencode.py
python3 .github/scripts/test_selfupdate.py
python3 .github/scripts/test_control.py
python3 .github/scripts/test_bench.py
python3 .github/scripts/test_path.py
python3 .github/scripts/test_tui.py    # app tests need Textual installed
```

`kiln tui` runs kiln commands rather than reimplementing them. A new
verb reaches it through `build_args` in `scripts/control.py`, the one
place a request becomes a command line. Keep everything in it a literal
or a registry key.

Open an issue before a large change so you do not build something that
does not fit the scope above.
