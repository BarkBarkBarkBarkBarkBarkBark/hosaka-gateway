# Hosaka Gateway

The small, app-neutral web front door for Hosaka. It discovers explicitly
approved web contributions in Hosaka's existing registry and serves them at
`/{extension_id}/`. Apps own their UI, domain logic and databases.

The native terminal/desktop backend is unchanged. The gateway does **not** proxy
PTYs, settings, installation, trust controls or arbitrary extension actions.
Web applications are trusted local code, not sandboxed processes.

## Native Linux/macOS installation

Keep the four checkouts together, or substitute absolute paths:

```sh
cd Hosaka
bash install.sh --tui \
  --gateway ../hosaka-gateway \
  --citations ../hosaka_citations \
  --virgozone ../virgo_zone
```

Omit `--tui` to install the existing graphical host too. Run as the owning user,
not root. Python 3.10+ and venv support are required; VirgoZone also uses ffmpeg
for media processing. Node is not required for the local web applications.

The optional installer creates a separate `.web-venv` in each selected checkout
and installs collision-safe commands in `$HOSAKA_PREFIX/bin` (default
`~/.local/bin`): `hosaka-gateway`, `cite`, `citations-setup`, `virgozone-start`.
Keep that directory on PATH. It never approves code, enables extensions, copies
databases, creates remote credentials or deploys the hosted wall.
Unmarked existing commands, including old Pi launchers, are never overwritten.
Back them up and rename them explicitly before installing their replacements.

To add web packages to an already installed Hosaka:

```sh
python3 hosaka-gateway/install.py --core "$PWD/Hosaka" --prefix "$HOME/.local" \
  --citations "$PWD/hosaka_citations" --virgozone "$PWD/virgo_zone"
```

## Inspect, link and explicitly approve

These are trusted-local grants, not an operating-system sandbox:

```sh
hosaka extensions link "$PWD/hosaka_citations/extension"
hosaka extensions trust citations --trusted-local \
  --allow-storage --allow-managed-data --allow-web \
  --web-source "$PWD/hosaka_citations/web/local" \
  --web-python "$PWD/hosaka_citations/.web-venv/bin/python"
hosaka extensions enable citations

hosaka extensions link "$PWD/virgo_zone/extension"
hosaka extensions trust virgo-zone --trusted-local --allow-storage \
  --allow-network --allow-web \
  --web-source "$PWD/virgo_zone/web/local" \
  --web-python "$PWD/virgo_zone/.web-venv/bin/python"
hosaka extensions enable virgo-zone

hosaka gateway configure
hosaka gateway serve
```

Open `http://127.0.0.1:8086/`, sign in, and select an app:

- `/citations/`: canonical managed citations SQLite (`/halloween-citations/` redirects here for compatibility).
- `/virgo-zone/`: VirgoZone social database.
- `/virgo-zone/citations`: legacy VirgoZone citation replica and local comments;
  this is deliberately **not** the canonical managed citation database.

`virgozone-start --no-browser` reuses a healthy gateway or starts it in the
foreground. Stop a foreground gateway with Ctrl-C; its app processes stop too.
`cite --help` documents the portable terminal workflows. The original Pi paths
and `/proc` dependency are no longer required.

No trust grant is implied by installing packages. Any extension or web-source
change requires inspection and reapproval. Revocation, disablement, safe mode
and changed source prevent routing and stop running apps. Optional network and
background grants remain separate; reapproval must retain each intended grant.
Native and web sync both use the same canonical citation data, but do not run
two periodic synchronizers for the same database simultaneously.

## Existing data and private app configuration

Run `hosaka paths --json` to locate the active data root. No database is moved.
If VirgoZone already has a source-local `data/app.db`, it refuses a silent switch
to a new empty database. Before starting it through the gateway, explicitly set
`DATA_DIR` to the existing absolute directory in a mode-0600 file:

```text
<data-root>/extension-data/virgo-zone/web-config.json
```

The JSON contains application environment **strings**, for example:

```json
{"DATA_DIR": "/absolute/existing/virgo-data", "CITATIONS_SOURCE": "/absolute/hosaka_citations/extension"}
```

Existing `.env` files are not automatically loaded by the gateway. Migrate
necessary application settings privately, never credentials into source.
Runtime/token/Python/path overrides are rejected. App config changes require
an app restart. Gateway auth lives privately at `gateway/auth.json`; rerunning
`configure` rotates the signing key and invalidates browser sessions.

## Phone/LAN access

The default is loopback-only. LAN binding requires explicit consent and TLS:

```sh
hosaka gateway serve --host 0.0.0.0 --port 8086 --allow-lan \
  --ssl-certfile /private/path/certificate.pem \
  --ssl-keyfile /private/path/key.pem
```

Use the machine's actual hostname/IP, not `0.0.0.0`, in the phone browser.
Install a certificate trusted by that phone. Login is required; state-changing
requests and WebSockets require a matching origin. Private app ports bind only
to loopback and require a per-process token. Proxy headers are not trusted.
Do not expose the separate native Hosaka backend to the LAN.

## App contract and operations

The extension manifest may declare:

```yaml
web:
  schema_version: 1
  file: server.py
  callable: app
  health: /health
```

The explicit web-source directory contains an ASGI application. The approved
interpreter must have Uvicorn and the app's dependencies installed. The gateway
starts one worker on an ephemeral loopback port, supplies ASGI `root_path`,
`HOSAKA_EXTENSION_DATA`, a private `HOSAKA_GATEWAY_TOKEN` and network/background
grant flags. Apps must verify the token on HTTP and WebSocket requests and
respect their grants. Authenticated requests carry `X-Hosaka-Role: organizer`.
Use prefix-relative URLs for assets, APIs, media and WebSockets.

Health/startup failure is visible in the catalog and private
`logs/gateway/<extension_id>.log`. Current output is capped at 2 MiB; an old
startup log may be retained separately. No shell command or arbitrary upstream
address is accepted from a browser. Runtime records for native read-only
adapters are private and removed when their worker stops.

Run tests with Hosaka available on PYTHONPATH:

```sh
PYTHONPATH=../Hosaka ../Hosaka/.hosakavenv/bin/python -m pytest -q tests
```

This checkout has local Linux validation only. Pi/macOS hardware, trusted-phone
TLS and production Supabase/Vercel cutover remain separate acceptance checks.

## Status, backup and updates

`curl --fail http://127.0.0.1:8086/health` checks the listener; the signed-in
catalog shows approval/startup errors. `cite status` checks citation sync state.
`hosaka extensions doctor --json` diagnoses the native registry.

Stop the gateway and any native background sync before coordinated backups or
updates. `citations-setup backup /private/path/citations.zip` produces a
consistent SQLite/evidence archive without user tokens; follow the citations
README for restore. Back up VirgoZone's separately configured data directory,
including its legacy citation replica, while its worker is stopped. Keep
private configuration outside source and never merge the two citation databases
by copying one over the other.

Update source/dependencies, inspect changes, validate and explicitly reapprove
each changed extension/web contribution, then restart. Installation does not
perform data cutover or remote deployment. See the citations performance report
for development-machine evidence and outstanding acceptance checks.
