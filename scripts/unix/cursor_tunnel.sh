#!/usr/bin/env bash
# Run the Cursor quick tunnel and keep Cursor's OpenAI workaround in
# step with it. cloudflared is a child of this process, not a pkill,
# so a different cloudflared (T3's named tunnel) is left alone.
# On the way out, cursor_sync.py down puts back the OpenAI settings Cursor
# had before up, if it still points at a trycloudflare URL. systemd also
# calls that on ExecStopPost.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SYNC=(python3 "$ROOT/scripts/unix/cursor_sync.py")
LOG="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/qwen-cursor-tunnel.log"
# Fedora setup drops cloudflared in /usr/local/bin; the WSL/Debian
# installer uses ~/.local/bin. Prefer PATH, then those two locations.
CF_BIN="$(command -v cloudflared 2>/dev/null || true)"
if [ -z "$CF_BIN" ]; then
    for candidate in /usr/local/bin/cloudflared "${HOME}/.local/bin/cloudflared"; do
        if [ -x "$candidate" ]; then
            CF_BIN="$candidate"
            break
        fi
    done
fi
if [ -z "$CF_BIN" ]; then
    echo "kiln: cloudflared not found (tried PATH, /usr/local/bin, ~/.local/bin)." >&2
    exit 1
fi
CF=(
    "$CF_BIN"
    tunnel --no-autoupdate --protocol quic --url http://127.0.0.1:8080
)

down() {
    "${SYNC[@]}" down || true
}
trap down EXIT

: >"$LOG"
"${CF[@]}" >>"$LOG" 2>&1 &
pid=$!

# A clean stop (SIGTERM from systemd) must exit 0 so Restart=on-failure
# does not bring the tunnel straight back. EXIT still runs `down`.
term() {
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    exit 0
}
trap term TERM INT

url=""
for _ in $(seq 1 60); do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "cloudflared exited before a public URL appeared." >&2
        tail -n 40 "$LOG" >&2 || true
        wait "$pid"
        exit $?
    fi
    url="$(grep -Eo 'https://[-a-z0-9]+\.trycloudflare\.com' "$LOG" | tail -n 1 || true)"
    if [ -n "$url" ]; then
        if ! "${SYNC[@]}" up --url "$url"; then
            echo "Cursor settings were not updated. The tunnel is still up." >&2
        fi
        echo "Cursor base URL: ${url}/v1"
        break
    fi
    sleep 1
done

if [ -z "$url" ]; then
    echo "No trycloudflare hostname after 60s. Leaving the tunnel process up." >&2
    tail -n 20 "$LOG" >&2 || true
fi

wait "$pid"
exit $?
