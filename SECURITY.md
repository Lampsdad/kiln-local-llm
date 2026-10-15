# Security

This repo starts a **network-listening inference server** and, optionally,
exposes it on the public internet through a Cloudflare quick tunnel. That
is the whole security surface, and it is worth understanding before you
run it.

## The threat model in one paragraph

`llama-server` binds `0.0.0.0:8080`, so it is reachable from every device
on your LAN. When you start a Cloudflare quick tunnel, it also becomes
reachable from **anywhere on the internet** at a
`*.trycloudflare.com` hostname. That hostname is not secret — it travels
over DNS and gets scraped. The generated API key is therefore the only
thing standing between an anonymous request and your GPU.

## What the scripts do about it

- Every start script generates a random key into `api_key.txt` (24 bytes
  from the platform CSPRNG) and passes it to llama-server with
  `--api-key-file`. Requests without a matching `Authorization` header are
  rejected.
- The file is locked to your account on every launch — `chmod 600` on
  Unix, `icacls /inheritance:r /grant:r` on Windows — so other accounts on
  a shared machine cannot read it. A key written before this behaviour
  existed gets re-locked the next time you start the server.
- `api_key.txt` is in `.gitignore`, and CI fails the build if it is ever
  tracked.
- `kiln opencode` does not copy the key into OpenCode's config. It writes
  a `{file:...}` reference to `api_key.txt`, which OpenCode reads at
  startup, so the credential stays in the one file that is locked down.
- `kiln pi` does not copy it into pi's `models.json` either. It writes
  the command `!cat '<repo>/api_key.txt'`, which pi runs on its first
  request to the server.
- `kiln self-update` runs only when you ask it to. It fast-forwards from
  the `origin` you cloned, and only to a tagged release unless you opted
  into `main`. The status board's daily check lists tags with
  `git ls-remote` and downloads nothing. Set `KILN_NO_UPDATE_CHECK=1`
  to turn the check off.
- `kiln tui` opens no network port. Its keys map to a fixed list of
  kiln commands, so nothing typed into it reaches a command line. Its
  benchmark starts every setup with the tunnel off (`KILN_NO_TUNNEL=1`)
  and stops the server when it finishes, so measuring never puts a
  server on a public URL.
- To start without the tunnel yourself, press `l` in `kiln tui`, or set
  `KILN_NO_TUNNEL=1` before `kiln start`. That removes the public URL.
  llama-server still listens on your LAN, behind the key.
- On Windows, a WSL or Docker container that publishes the same port
  takes `127.0.0.1:8080` ahead of llama-server. The tunnel forwards to
  `localhost`, so it would publish that program instead of the model.
  `kiln tui` warns when it sees this, and finds llama-server by the
  machine's name instead.

```bat
kiln key show           :: print it (generates it on first use)
kiln key rotate         :: discard and regenerate
kiln key set MY-SECRET  :: use a passphrase of your own
```

Rotating takes effect on the next server start, and you must paste the new
value into Cursor. OpenCode and pi pick it up on their own next start.

## What the scripts do *not* do about it

Be clear-eyed about the limits:

- **A quick tunnel has no access control of its own.** Anyone with the
  hostname and the key gets in; anyone with the hostname alone can still
  reach the endpoint and consume connection resources.
- **There is no rate limiting** in front of the server.
- **The key is passed to Cursor in plaintext** and stored in Cursor's
  settings.
- **The abliterated build has had its refusal behaviour removed.** If you
  expose that one through a tunnel, the API key is the only thing
  preventing an anonymous party from using an unrestricted model that is
  attributable to your IP. Think about this before you tunnel it.

For anything beyond occasional personal use, register a
[named Cloudflare tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
and put a **Cloudflare Access** policy in front of it. That gives you a
stable hostname plus real authentication, instead of a shared secret on a
public URL. If you only ever use the model from the same machine, do not
start a tunnel at all.

## Reporting a vulnerability

If you find a problem in **these scripts** — a leaked key, a permissions
mistake, a command injection in a launcher — please report it privately
via [GitHub's private vulnerability reporting](../../security/advisories/new)
rather than a public issue. I will usually respond within a week.

Vulnerabilities in **llama.cpp**, **cloudflared** or **Cursor** should go
to those projects directly; this repo only orchestrates them.

## Supported versions

This is a small script collection with no release branches — fixes land on
`main`. Pull before reporting.
