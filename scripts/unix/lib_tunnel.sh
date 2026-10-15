# shellcheck shell=bash
# ============================================================
#  kiln's own Cloudflare quick tunnel, and no one else's.
#
#  Sourced by kiln.sh, the start scripts and slurm-llama.sh.
#
#    kiln_tunnel_pids PORT   cloudflared processes forwarding to
#                            PORT, one PID a line
#    kiln_stop_tunnel PORT   stop them; returns 1 if there were none
#    kiln_cursor_follow URL  point Cursor at it (see below)
#    kiln_cursor_unfollow    and turn that off again
#
#  Other programs run cloudflared too: a named tunnel, T3 Code's
#  remote access, another project's quick tunnel. `pkill -x
#  cloudflared` stopped every one of them. kiln's tunnel is the
#  quick tunnel to kiln's own port, started as
#  `cloudflared tunnel --url http://127.0.0.1:PORT` (or localhost,
#  as the macOS and Fedora scripts and older kiln write it), so
#  that is all these touch.
# ============================================================

kiln_tunnel_pids() {
    local pid comm
    # pgrep -f matches the command line, which for a script is its
    # interpreter's too; ps then confirms the program is cloudflared.
    for pid in $(pgrep -f -- "tunnel .*--url http://(localhost|127\\.0\\.0\\.1):$1( |\$)" 2>/dev/null); do
        comm="$(ps -o comm= -p "$pid" 2>/dev/null)" || continue
        # macOS prints the full path here, Linux the bare name.
        [ "${comm##*/}" = cloudflared ] && echo "$pid"
    done
    return 0
}

kiln_stop_tunnel() {
    local pids
    pids="$(kiln_tunnel_pids "$1")"
    [ -n "$pids" ] || return 1
    # shellcheck disable=SC2086  # one PID per word
    kill $pids 2>/dev/null
    return 0
}

# Cursor follows kiln's tunnel once `kiln cursor` has been run in this
# checkout, which leaves .kiln/cursor.json. These never fail the caller:
# the server is up, or down, whatever Cursor makes of it.
#
#    kiln_cursor_follow URL   point Cursor at a new tunnel (URL ends /v1)
#    kiln_cursor_unfollow     turn Cursor's override off again
kiln_cursor_follow() {
    [ -f .kiln/cursor.json ] && [ -n "${KILN_PY:-}" ] && [ -n "$1" ] || return 0
    "$KILN_PY" scripts/cursor.py --follow "$1" || true
}

kiln_cursor_unfollow() {
    [ -f .kiln/cursor.json ] && [ -n "${KILN_PY:-}" ] || return 0
    "$KILN_PY" scripts/cursor.py --unfollow || true
}
