# Running KDIVE under systemd

Run the three KDIVE processes as host services. The unit files live under
[`deploy/systemd/`](../../deploy/systemd/): system-scope units in
[`deploy/systemd/system/`](../../deploy/systemd/system/) and user-scope units in
[`deploy/systemd/user/`](../../deploy/systemd/user/).

## External-backend prerequisite

The units run only the KDIVE processes. Postgres, the S3-compatible object store, and the
OIDC issuer are external and are not ordered by these units — a process retries until its
backends are reachable rather than failing terminally. If you co-locate a backend on the
same host, ordering the KDIVE units after it is the operator's responsibility: add the
appropriate `After=`/`Wants=` via a drop-in. Run the provider preflight (see
[install](install.md)) before the first start.

## System scope

Install the package under `/opt/kdive` with its `.venv`, create the service user, and place
the environment file:

```bash
sudo useradd --system --home-dir /opt/kdive --shell /usr/sbin/nologin kdive
sudo install -d -o kdive -g kdive /etc/kdive
sudo install -m 0640 -o kdive -g kdive \
  deploy/systemd/kdive.env.example /etc/kdive/kdive.env
```

Edit `/etc/kdive/kdive.env` and fill in the `KDIVE_*` values and credentials from your
secret store; the file ships credential-less by design. Every name in it is a registered
setting documented in [the config reference](../guide/reference/config.md). Keep the file
at mode 0640 owned by `kdive` so secrets are not world-readable.

Install and enable the units:

```bash
sudo cp deploy/systemd/system/kdive-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kdive-server kdive-worker kdive-reconciler
```

Follow the logs:

```bash
journalctl -u kdive-server -f
```

## User scope

The `--user` variant runs the same processes without root, reading the environment from
`~/.config/kdive/kdive.env` and the venv from `~/.local/share/kdive/.venv`:

```bash
install -d ~/.config/kdive
install -m 0640 deploy/systemd/kdive.env.example ~/.config/kdive/kdive.env
cp deploy/systemd/user/kdive-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kdive-server kdive-worker kdive-reconciler
journalctl --user -u kdive-server -f
```

A short install summary also lives next to the units in
[`deploy/systemd/README.md`](../../deploy/systemd/README.md).

## Debugging the MCP transport layer

Set `KDIVE_MCP_TRACE=1` in `kdive.env` to log one line per HTTP request to the server's
journal — method, path, whether an `Mcp-Session-Id` was present (and its value), the
`MCP-Protocol-Version`, the response status, and `duration_ms`. It is enough to reconstruct
an MCP session lifecycle (initialize → requests → a `404 Session not found` → whether the
client re-`initialize`s) from server logs alone ([ADR-0417](../adr/0417-opt-in-asgi-transport-trace.md)).

- **Off by default; debug-only.** It emits a line per request, so leave it unset in normal
  operation. It is a diagnostic aid, not standing telemetry.
- **Restart to take effect, and arm it first.** The flag is read at server start, so
  `systemctl restart kdive-server` after setting it. The restart drops in-memory MCP
  sessions — so enable it *before* reproducing an issue; the trace captures newly-established
  sessions, not the one currently wedged.
- **Emits regardless of `KDIVE_LOG_LEVEL`.** The trace lines appear whenever the flag is on,
  even under `KDIVE_LOG_LEVEL=warning`.
- **Never logs the bearer token.** The `Authorization` header is recorded as a presence flag
  only. The `Mcp-Session-Id` (a session handle, not an auth credential) is logged as its
  value so a server line correlates to a specific client session.

```bash
journalctl -u kdive-server -f | grep transport_trace
```
