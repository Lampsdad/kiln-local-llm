#!/usr/bin/env bash
# ============================================================
#  kiln -- macOS / Linux front door.
#
#    ./kiln.sh                full-screen TUI
#    ./kiln.sh status         status board
#    ./kiln.sh setup          llama.cpp + cloudflared + models/
#    ./kiln.sh hardware       what this GPU can run
#    ./kiln.sh get [VARIANT|both]
#    ./kiln.sh start [VARIANT] [--no-mtp]
#    ./kiln.sh stop           stop server and tunnel
#    ./kiln.sh key [show|rotate|set KEY]
#    ./kiln.sh opencode       list the local models in OpenCode
#    ./kiln.sh pi             list the local models in pi
#    ./kiln.sh self-update    update kiln itself from GitHub
#    ./kiln.sh tui            full-screen app, with a benchmark
#    ./kiln.sh bench | quality | clean | help
#    ./kiln.sh path [--remove] put kiln on PATH (~/.local/bin)
#
#  Bare `kiln` opens the TUI. `kiln status` prints the board.
#  This dispatches to the per-platform scripts in scripts/unix/.
#  It deliberately holds no launch logic of its own -- those
#  scripts stay the single source of truth.
# ============================================================
set -uo pipefail

# A ~/.local/bin/kiln symlink has to resolve to this checkout, not
# to ~/.local/bin. macOS readlink has no -f on older releases, so
# walk the chain. Relative targets are joined to the link's directory.
_kiln_src="${BASH_SOURCE[0]}"
_kiln_n=0
while [ -L "$_kiln_src" ]; do
    _kiln_n=$((_kiln_n + 1))
    if [ "$_kiln_n" -gt 20 ]; then
        echo "kiln: too many symlinks resolving ${_kiln_src}" >&2
        exit 1
    fi
    _kiln_dir="$(cd -P "$(dirname "$_kiln_src")" && pwd -P)" || exit 1
    _kiln_next="$(readlink "$_kiln_src")" || exit 1
    case "$_kiln_next" in
        /*) _kiln_src="$_kiln_next" ;;
        *)  _kiln_src="${_kiln_dir}/${_kiln_next}" ;;
    esac
done
ROOT="$(cd -P "$(dirname "$_kiln_src")" && pwd -P)" || {
    echo "kiln: cannot find the repo from ${_kiln_src}" >&2
    exit 1
}
unset _kiln_src _kiln_dir _kiln_next _kiln_n
UNIX="$ROOT/scripts/unix"
cd "$ROOT"

# ---- platform ----------------------------------------------
case "$(uname -s)" in
    Darwin) PLATFORM=mac ;;
    Linux)
        # Fedora/RHEL need dnf and a different CUDA package path,
        # which install_fedora.sh handles.
        if [ -r /etc/os-release ] &&
           grep -qiE '^(ID|ID_LIKE)=.*(fedora|rhel|centos)' /etc/os-release; then
            PLATFORM=fedora
        else
            PLATFORM=linux
        fi
        ;;
    *)
        echo "kiln.sh supports macOS and Linux. On Windows use:  kiln" >&2
        exit 1
        ;;
esac

INSTALL="$UNIX/install_${PLATFORM}.sh"
START="$UNIX/start_${PLATFORM}.sh"

have() { command -v "$1" >/dev/null 2>&1; }

# The port the start script serves on. kiln's server and tunnel are
# found by it, so the status board and kiln stop agree on which
# processes are kiln's.
server_port() {
    local port
    port="$(sed -n 's/^PORT=\([0-9][0-9]*\)$/\1/p' "$START" 2>/dev/null | head -1)"
    echo "${port:-8080}"
}

# hardware.py sizes every launch and holds the model registry.
# Empty when Python is absent; every caller below degrades rather
# than failing, so kiln still works on a machine that only has
# weights already on disk. lib_python.sh rejects the Windows
# Store stub, which is on PATH as "python3" but runs nothing.
. "$UNIX/lib_python.sh"
PY="$KILN_PY"
# shellcheck source=scripts/unix/lib_server.sh
. "$UNIX/lib_server.sh"

# Every family the registry knows. Qwen3.8 is what kiln picks for
# you; Gemma 4 runs only when named.
VARIANTS="base, ablit, 9b, 4b, gemma-31b, gemma-26b, gemma-12b, gemma-e4b"

# A family id hardware.py knows. The Qwen ids pass without Python
# too, so lib_variants.sh can say what is missing.
is_variant() {
    case "$1" in
        ""|-*)            return 1 ;;
        base|ablit|9b|4b) return 0 ;;
    esac
    [ -n "$PY" ] && "$PY" "$ROOT/scripts/hardware.py" --variant "$1" >/dev/null 2>&1
}

# ---- status board ------------------------------------------
status() {
    local bin="./llama-bin/llama-server" bin_help=""
    printf '\n'
    sed 's/^/  /' "$ROOT/assets/banner.txt" 2>/dev/null || true
    printf '\n'
    printf ' %s\n' '------------------------------------------------------------'

    # kiln itself: the version, and whether GitHub has a newer one.
    # selfupdate.py asks at most once a day and gives up after a few
    # seconds offline, so this is free on almost every render.
    local kv="" ku=""
    if [ -n "$PY" ]; then
        while IFS='=' read -r k v; do
            case "$k" in
                KILN_VERSION) kv="$v" ;;
                KILN_UPDATE)  ku="$v" ;;
            esac
        done < <("$PY" "$ROOT/scripts/selfupdate.py" --notice 2>/dev/null \
                     | kiln_strip_cr || true)
    fi
    if [ -n "$ku" ]; then
        printf '  %-14s %s\n' "kiln" "${kv:-unknown}  $ku available  ./kiln.sh self-update"
    else
        printf '  %-14s %s\n' "kiln" "${kv:-unknown}"
    fi

    local build=0
    if [ -x "$bin" ]; then
        if [ -n "$PY" ]; then
            build="$("$PY" "$ROOT/scripts/hardware.py" --build "$bin" 2>/dev/null |
                     kiln_strip_cr | sed -n 's/^BUILD=//p' | head -1)"
        fi
        if [ "${build:-0}" -gt 0 ]; then
            printf '  %-14s %s\n' "llama.cpp" "installed  build $build"
        else
            printf '  %-14s %s\n' "llama.cpp" "installed"
        fi
        # Read whole, not piped into grep -q, which quits at the first
        # match and fails the pipeline under pipefail. See lib_select.sh.
        bin_help="$("$bin" --help 2>&1 || true)"
        case "$bin_help" in
            *draft-mtp*)
                printf '  %-14s %s\n' "MTP support" "available" ;;
            *)
                printf '  %-14s %s\n' "MTP support" "unavailable  build predates b9180" ;;
        esac
    else
        printf '  %-14s %s\n' "llama.cpp" "not installed    ./kiln.sh setup"
    fi

    if have cloudflared; then
        printf '  %-14s %s\n' "cloudflared" "installed"
    else
        printf '  %-14s %s\n' "cloudflared" "not installed"
    fi

    if have nvidia-smi; then
        printf '  %-14s %s\n' "GPU" \
            "$(nvidia-smi "--query-gpu=name,memory.total" --format=csv,noheader 2>/dev/null | head -1)"
    elif [ "$PLATFORM" = mac ]; then
        # Metal is Apple Silicon only. llama.cpp's Intel build runs on the CPU.
        if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
            printf '  %-14s %s\n' "GPU" "$(sysctl -n machdep.cpu.brand_string 2>/dev/null) (Metal)"
        else
            printf '  %-14s %s\n' "GPU" "$(sysctl -n machdep.cpu.brand_string 2>/dev/null) (CPU only)"
        fi
    fi

    # What this card can actually run -- answers "will it run on my
    # GPU" on the board itself, rather than sending the reader to a
    # table to match their card by name.
    local tier="" rq="" rf="" rc=0
    if [ -n "$PY" ]; then
        while IFS='=' read -r k v; do
            case "$k" in
                TIER_LABEL) tier="$v" ;;
                REC_FAMILY) rf="$v" ;;
                REC_QUANT)  rq="$v" ;;
                REC_CTX)    rc="$v" ;;
            esac
        done < <("$PY" "$ROOT/scripts/hardware.py" --recommend 2>/dev/null \
                     | kiln_strip_cr || true)
    fi
    if [ -n "$tier" ] && [ -n "$rq" ]; then
        printf '  %-14s %s\n' "profile" "$tier  runs $rf $rq at $((rc / 1024))K ctx"
    elif [ -n "$tier" ]; then
        printf '  %-14s %s\n' "profile" "$tier  nothing here fits"
    fi

    printf ' %s\n' '------------------------------------------------------------'
    local found=0
    for f in ./models/Qwen3.8-27B-*.gguf; do
        [ -e "$f" ] || continue
        printf '  %-14s %s\n' "Qwen3.8-27B" "$(basename "$f")"; found=1
    done
    for f in ./models/Huihui-Qwen3.8-27B-abliterated-*.gguf; do
        [ -e "$f" ] || continue
        printf '  %-14s %s\n' " abliterated" "$(basename "$f")"; found=1
    done
    for f in ./models/Qwen3.8-9B-*.gguf; do
        [ -e "$f" ] || continue
        printf '  %-14s %s\n' " 9B distill" "$(basename "$f")"; found=1
    done
    for f in ./models/Qwen3.8-4B-*.gguf; do
        [ -e "$f" ] || continue
        printf '  %-14s %s\n' " 4B distill" "$(basename "$f")"; found=1
    done
    # Gemma 4 is opt-in, so it has rows only once something is on
    # disk. Its MTP heads and projectors are named mtp-gemma-4-* and
    # mmproj-gemma-4-*, so this glob only finds weights.
    local gemma=0
    for f in ./models/gemma-4-*.gguf; do
        [ -e "$f" ] || continue
        printf '  %-14s %s\n' "Gemma 4" "$(basename "$f")"; found=1; gemma=1
    done
    [ "$found" = 0 ] && printf '  %-14s %s\n' "weights" "none         ./kiln.sh get"

    printf ' %s\n' '------------------------------------------------------------'
    local port running
    port="$(server_port)"
    running="$(kiln_server_pids "$port")"
    if [ -n "$running" ]; then
        printf '  %-14s %s\n' "server" "running"
    else
        printf '  %-14s %s\n' "server" "stopped"
    fi
    if [ -f ./api_key.txt ]; then
        local k; k="$(tr -d '\r\n' < ./api_key.txt)"
        printf '  %-14s %s...%s   ./kiln.sh key show\n' "API key" \
            "${k:0:6}" "${k: -4}"
    else
        printf '  %-14s %s\n' "API key" "not generated yet"
    fi
    printf ' %s\n\n' '------------------------------------------------------------'

    # The first unmet dependency, so following it repeatedly walks
    # an empty checkout all the way to a served model.
    local gmin=0
    if [ "$gemma" = 1 ] && [ -n "$PY" ]; then
        gmin="$("$PY" "$ROOT/scripts/hardware.py" --variant gemma-12b 2>/dev/null |
                kiln_strip_cr | sed -n 's/^V_MIN_BUILD=//p' | head -1)"
    fi
    if   [ ! -x "$bin" ];   then echo " Next:  ./kiln.sh setup    install llama.cpp"
    elif [ "$found" = 0 ];  then echo " Next:  ./kiln.sh get      download the weights"
    elif [ "${build:-0}" -gt 0 ] && [ "${build:-0}" -lt "${gmin:-0}" ]; then
        echo " Next:  mv llama-bin llama-bin-backup && ./kiln.sh setup"
        echo "        Gemma 4 needs llama.cpp b${gmin} or newer; this is b${build}."
    elif [ -z "$running" ]; then
                                 echo " Next:  ./kiln.sh start    serve a model to Cursor"
    else                         echo " Server is up. Point Cursor at the URL it printed."
    fi
    printf '\n'
}

# No arguments open the TUI. A missing Python, or a non-terminal,
# cannot run it: say why and print the board. stdin counts too --
# `kiln </dev/null` still has a terminal stdout, and the app would
# block waiting on it.
open_tui() {
    if [ ! -t 0 ] || [ ! -t 1 ]; then
        echo "kiln: not a terminal, so the TUI cannot start. Showing the status board." >&2
        status
        return 0
    fi
    if [ -z "$PY" ]; then
        echo "kiln: no Python, so the TUI cannot start. Showing the status board." >&2
        status
        return 0
    fi
    exec "$PY" "$ROOT/scripts/tui.py"
}

usage() {
    cat <<'USAGE'

 Usage:  kiln COMMAND [ARGUMENT]     (./kiln.sh is the same command)

   status            print the status board: what is installed,
                     downloaded and running
   setup             llama.cpp, cloudflared and models/
   hardware          what this GPU can run, and at what context
   get [VARIANT|both]
                     download weights; no argument takes the build
                     this GPU is sized for
   start [VARIANT] [--no-mtp]
                     serve a model to Cursor; --no-mtp trades the
                     MTP draft head for a larger context window
   stop              stop llama-server and cloudflared
   key [show|rotate|set KEY]
                     the API key Cursor needs; set KEY uses yours instead
   opencode          list the local models in OpenCode on this machine
                     (--print to preview, --remove to undo,
                     --url URL to pin the server address)
   pi                list the local models in pi on this machine
                     (the same --print, --remove and --url)
   self-update       update kiln itself to the newest release
                     (--main to follow every change on main,
                     --check to only report what is available)
   tui               full-screen app, with an interactive benchmark
                     (the default: kiln with no command does this)
   bench             speed benchmarks
   quality           KL-divergence of each quant vs Q8_0
   clean             reclaim space from superseded GGUFs
   path [--remove]   put kiln on PATH, so it runs from any directory
                     (setup does this; --remove undoes it)
   help              this text

 With no command, kiln opens the TUI.
 kiln status prints the status board.
 If the TUI cannot start (no Python, or not a terminal), kiln
 prints a short reason and then the status board.

 Variants:
   base ablit 9b 4b       Qwen3.8: 27B, 27B abliterated, 9B and 4B
                          distills. kiln picks from these for you.
   gemma-31b gemma-26b    Gemma 4: 31B dense, 26B-A4B MoE, 12B and
   gemma-12b gemma-e4b    E4B. Only when named. Needs llama.cpp b11059+.

USAGE
}

CMD="${1:-}"
ARG="${2:-}"

case "$CMD" in
    "")         open_tui ;;
    status)     status ;;
    setup)      exec "$INSTALL" ;;
    get)
        case "$ARG" in
            both)
                # Stock first: it brings down the MTP head and the
                # vision projector, which the abliterated run then
                # finds present and skips.
                VARIANT=base  "$UNIX/download.sh" || exit $?
                VARIANT=ablit exec "$UNIX/download.sh"
                ;;
            "")         exec "$UNIX/download.sh" ;;
            *)
                if is_variant "$ARG"; then
                    exec env VARIANT="$ARG" "$UNIX/download.sh"
                fi
                echo "kiln: unknown variant '$ARG' (use $VARIANTS or both)" >&2
                exit 1
                ;;
        esac
        ;;
    start)
        # Either order, as start.bat takes them: start ablit --no-mtp.
        # The first variant wins, as it does there.
        shift
        WANT=""
        for a in "$@"; do
            if [ "$a" = --no-mtp ]; then
                export KILN_NO_MTP=1
            elif is_variant "$a"; then
                [ -n "$WANT" ] || WANT="$a"
            else
                echo "kiln: unknown start option '$a' (use $VARIANTS or --no-mtp)" >&2
                exit 1
            fi
        done
        if [ -n "$WANT" ]; then exec env VARIANT="$WANT" "$START"; fi
        exec "$START"
        ;;
    stop)
        # Only kiln's own server and tunnel, on the port the start script
        # serves. Another llama-server, a named tunnel or T3 Code's remote
        # access are left running.
        port="$(server_port)"
        kiln_stop_server "$port" && echo " stopped llama-server" || echo " llama-server was not running"
        # shellcheck source=scripts/unix/lib_tunnel.sh
        . "$UNIX/lib_tunnel.sh"
        kiln_stop_tunnel "$port" && echo " stopped cloudflared" || true
        ;;
    key)
        case "$ARG" in
            set)
                # Your own key in place of the generated one, as kiln.bat does.
                # llama-server compares it verbatim, so any single line will do.
                # An empty value is no key at all, and a line break is cut off
                # when the file is read back, so neither is accepted.
                VALUE="${3:-}"
                case "$VALUE" in
                    "")
                        echo "usage: kiln key set KEY" >&2
                        exit 1
                        ;;
                    *$'\n'*|*$'\r'*)
                        echo "usage: kiln key set KEY  (one line, no line breaks)" >&2
                        exit 1
                        ;;
                esac
                # Written to a new file and renamed over the old one: writing
                # into an existing file keeps its mode, which may be
                # world-readable, and the new key would show through until a
                # chmod caught up.
                if ! ( umask 077; printf '%s' "$VALUE" > ./api_key.txt.tmp ) ||
                   ! mv -f ./api_key.txt.tmp ./api_key.txt; then
                    rm -f ./api_key.txt.tmp
                    echo "kiln: could not write api_key.txt" >&2
                    exit 1
                fi
                echo " key set"
                echo " Restart the server to load it."
                ;;
            ""|show|rotate)
                [ "$ARG" = rotate ] && rm -f ./api_key.txt
                # shellcheck source=scripts/unix/lib_api_key.sh
                . "$UNIX/lib_api_key.sh"
                echo
                echo " API key -- paste into Cursor's OpenAI API Key field"
                echo
                echo "   $API_KEY"
                echo
                echo " Stored in api_key.txt, readable only by you."
                echo
                ;;
            *)
                echo "usage: kiln key [show|rotate|set KEY]" >&2
                exit 1
                ;;
        esac
        ;;
    hardware|hw)
        # Its own command rather than more rows on the board: this is
        # the answer to "will it run on my GPU", and it is meant to be
        # pasteable into a hardware report.
        if [ -z "$PY" ]; then
            echo "kiln: needs python3 to size a launch." >&2
            exit 1
        fi
        echo
        echo " This machine"
        echo " ------------------------------------------------------------"
        "$PY" "$ROOT/scripts/hardware.py" --detect | kiln_strip_cr |
                while IFS='=' read -r k v; do
            case "$k" in
                GPU_NAME)   [ -n "$v" ] && printf '  %-11s %s\n' "GPU" "$v" ;;
                VRAM_MIB)   printf '  %-11s %s MiB\n' "VRAM" "$v" ;;
                CORES)      printf '  %-11s %s physical\n' "cores" "$v" ;;
                RAM_MIB)    printf '  %-11s %s MiB\n' "RAM" "$v" ;;
                TIER_LABEL) printf '  %-11s %s\n' "profile" "$v" ;;
            esac
        done
        echo " ------------------------------------------------------------"
        echo
        echo " What it should run"
        echo " ------------------------------------------------------------"
        "$PY" "$ROOT/scripts/hardware.py" --recommend | kiln_strip_cr |
                while IFS='=' read -r k v; do
            case "$k" in
                REC_LABEL)    printf '  %-11s %s\n' "model" "$v" ;;
                REC_QUANT)    printf '  %-11s %s\n' "quant" "$v" ;;
                REC_CTX)      printf '  %-11s %s tokens\n' "context" "$v" ;;
                REC_MTP)      printf '  %-11s %s\n' "MTP head" "${v:-off}" ;;
                REC_MMPROJ)   printf '  %-11s %s\n' "vision" "${v:-off}" ;;
                REC_TOTAL_GB) printf '  %-11s ~%s GB\n' "download" "$v" ;;
                REC_THIRD_PARTY)
                    [ "$v" = 1 ] && printf '  %-11s %s\n' "note" \
                        "third-party distill, not a Qwen release" ;;
                REC_BELOW_MIN)
                    [ "$v" = 1 ] && printf '  %-11s %s\n' "note" \
                        "below the context kiln aims for; it will still load" ;;
                REC_BELOW_QUALITY)
                    [ "$v" = 1 ] && printf '  %-11s %s\n' "note" \
                        "a low-bit quant, below the quality floor; expect weaker output" ;;
                REC_REASON)   printf '  %-11s %s\n' "no fit" "$v" ;;
            esac
        done
        echo " ------------------------------------------------------------"
        echo
        echo " ./kiln.sh get   downloads exactly this"
        echo " Set KILN_MIN_CTX to trade context against quant quality."
        echo
        ;;
    opencode)
        if [ -z "$PY" ]; then
            echo "kiln: needs python3 to write the OpenCode config." >&2
            exit 1
        fi
        # The config references api_key.txt rather than copying it,
        # and OpenCode will not start if that file is missing.
        # shellcheck source=scripts/unix/lib_api_key.sh
        . "$UNIX/lib_api_key.sh"
        shift
        exec "$PY" "$ROOT/scripts/opencode.py" "$@"
        ;;
    pi)
        if [ -z "$PY" ]; then
            echo "kiln: needs python3 to write pi's models.json." >&2
            exit 1
        fi
        # pi reads api_key.txt on its first request to the server, so
        # make sure there is one to read.
        # shellcheck source=scripts/unix/lib_api_key.sh
        . "$UNIX/lib_api_key.sh"
        shift
        exec "$PY" "$ROOT/scripts/pi_agent.py" "$@"
        ;;
    self-update)
        if [ -z "$PY" ]; then
            echo "kiln: needs python3 to update itself. Or by hand: git pull" >&2
            exit 1
        fi
        # exec, so bash is not left reading a kiln.sh the update has
        # just replaced.
        shift
        exec "$PY" "$ROOT/scripts/selfupdate.py" "$@"
        ;;
    tui)
        if [ -z "$PY" ]; then
            echo "kiln: needs python3 for the TUI." >&2
            exit 1
        fi
        shift
        exec "$PY" "$ROOT/scripts/tui.py" "$@"
        ;;
    bench)    exec "$UNIX/benchmark.sh" ;;
    quality)  exec "$UNIX/quality.sh" ;;
    clean)    exec "$UNIX/cleanup.sh" ;;
    path)     shift; exec "$UNIX/kiln_path.sh" "$@" ;;
    help|-h|--help) usage ;;
    *)  echo "kiln: unknown command '$CMD'" >&2; usage; exit 1 ;;
esac
