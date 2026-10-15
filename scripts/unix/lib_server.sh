# shellcheck shell=bash
# ============================================================
#  kiln's own llama-server, and the public address it is served at.
#
#  Sourced by kiln.sh, the start scripts, benchmark.sh and
#  quality.sh.
#
#    kiln_server_pids PORT        llama-server processes serving PORT,
#                                 one PID a line
#    kiln_stop_server PORT        stop them and wait for the memory to
#                                 be freed; returns 1 if there were none
#    kiln_wait_tunnel_url LOG PID the quick-tunnel address cloudflared
#                                 prints into LOG, or nothing
#    kiln_shutdown SERVER CF      the Ctrl+C handler of the start scripts
#
#  Matching llama-server by name stopped every llama-server on the
#  machine, including another project's and another kiln checkout's on
#  another port. kiln's server is the one whose command line has
#  `--port PORT` for kiln's own port, which is how the start scripts
#  launch it.
# ============================================================

kiln_server_pids() {
    local pid comm
    # pgrep -f matches the command line, which for a script is its
    # interpreter's too; ps then confirms the program is llama-server.
    for pid in $(pgrep -f -- "llama-server.* --port $1( |\$)" 2>/dev/null); do
        comm="$(ps -o comm= -p "$pid" 2>/dev/null)" || continue
        # macOS prints the full path here, Linux the bare name.
        [ "${comm##*/}" = llama-server ] && echo "$pid"
    done
    return 0
}

kiln_stop_server() {
    local pids left i
    pids="$(kiln_server_pids "$1")"
    [ -n "$pids" ] || return 1
    # shellcheck disable=SC2086  # one PID per word
    kill $pids 2>/dev/null || true
    # The next launch needs the memory this one holds, so wait for the
    # process to go rather than sleep for a guessed time.
    i=0
    while [ "$i" -lt 10 ]; do
        left="$(kiln_server_pids "$1")"
        [ -n "$left" ] || return 0
        sleep 1
        i=$((i + 1))
    done
    # shellcheck disable=SC2086  # one PID per word
    kill -9 $left 2>/dev/null || true
    return 0
}

kiln_wait_tunnel_url() {
    local log="$1" pid="$2" url="" n=0
    # cloudflared prints its address a few seconds after it starts, and
    # later on a slow link. Stop early if it has already died.
    while [ "$n" -lt 30 ]; do
        url="$(grep -o 'https://[^ ]*trycloudflare\.com' "$log" 2>/dev/null | head -1 || true)"
        [ -n "$url" ] && break
        kill -0 "$pid" 2>/dev/null || break
        sleep 2
        n=$((n + 1))
    done
    echo "$url"
}

kiln_shutdown() {
    echo
    echo "Shutting down..."
    kill "$1" 2>/dev/null || true
    if [ -n "${2:-}" ]; then kill "$2" 2>/dev/null || true; fi
    exit 0
}
