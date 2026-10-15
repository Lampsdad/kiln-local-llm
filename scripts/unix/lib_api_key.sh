#!/usr/bin/env bash
# ============================================================
#  Shared API-key handling for the start_*.sh scripts.
#
#  Sourced, not executed:  . ./lib_api_key.sh
#
#  Why this exists: the Cloudflare quick-tunnel URL is public.
#  Without a key, anyone who guesses or scrapes the hostname gets
#  a free OpenAI-compatible endpoint pointed at your GPU. Cursor
#  makes you fill in an API-key field anyway, so requiring one
#  costs nothing.
#
#  Sets:  API_KEY       the key itself
#         API_KEY_FILE  path to pass to --api-key-file
# ============================================================

API_KEY_FILE="./api_key.txt"

if [ ! -f "$API_KEY_FILE" ]; then
    echo "Generating an API key for this server (${API_KEY_FILE})..."
    # Each write runs under umask 077, so the file is never readable by
    # anyone else, not even for an instant. The subshells keep the umask
    # from reaching the rest of the start script.
    if command -v openssl >/dev/null 2>&1; then
        ( umask 077; openssl rand -hex 24 | tr -d '\n' > "$API_KEY_FILE" )
    elif [ -r /dev/urandom ]; then
        # od is in POSIX; xxd is not guaranteed to be installed.
        ( umask 077; od -An -tx1 -N24 /dev/urandom | tr -d ' \n' > "$API_KEY_FILE" )
    elif command -v uuidgen >/dev/null 2>&1; then
        # A uuid is only 16 bytes, so take two and keep 24 bytes' worth.
        ( umask 077; { uuidgen; uuidgen; } | tr -d '\n-' | tr '[:upper:]' '[:lower:]' \
          | cut -c1-48 | tr -d '\n' > "$API_KEY_FILE" )
    else
        echo "ERROR: no way to generate a random key (need openssl, /dev/urandom, or uuidgen)."
        echo "       Write one yourself:  echo mysecret > $API_KEY_FILE"
        exit 1
    fi
fi

# The key is a credential; keep it off other accounts on the box. This
# runs on every launch, not only at creation, so a key file from an
# older kiln or written by hand gets locked down too.
chmod 600 "$API_KEY_FILE" 2>/dev/null || true

API_KEY="$(tr -d '\r\n' < "$API_KEY_FILE")"

if [ -z "$API_KEY" ]; then
    echo "ERROR: $API_KEY_FILE is empty. Delete it and re-run to regenerate."
    exit 1
fi
