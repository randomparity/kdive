# Example: local-libvirt developer setup

A reference for the primary local-libvirt use case — a developer standing up KDIVE on
their own workstation and driving a kernel through its build → boot → debug → capture
lifecycle from an MCP client.

The three KDIVE processes run **as root against `qemu:///system`** (the representative
identity for managing system-scope QEMU/KVM domains, libguestfs, kexec, and the console
log), the stack onboards a project named **`local`**, and an MCP client opened in your
kernel tree (`~/src/linux` by default) drives that very checkout.

This example calls the real product commands (`docker compose`, `python -m kdive migrate`,
`python -m kdive seed-demo`) rather than the source-tree `just` recipes, so it mirrors a
real deployment. For the underlying reference material see
[`docs/operating/local-stack.md`](../../docs/operating/local-stack.md), the
[local-libvirt walkthrough](../../docs/operating/providers/local-libvirt-walkthrough.md),
and the [four-method live run](../../docs/operating/runbooks/four-method-live-run.md).

## Prerequisites

- A KVM host with `libvirt` and a running `libvirtd`/`virtqemud`, the `default` network
  active, and your user in the `libvirt` group.
- Docker with a reachable daemon (for the Postgres / MinIO / mock-OIDC backends).
- The repo synced (`uv sync --locked`) so `.venv/bin/python` can `import kdive`.
- An operator-built guest image at
  `/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2` (owned so the `qemu` user can
  read it). Build one with `python -m kdive build-fs` — see the
  [live-stack runbook §3](../../docs/operating/runbooks/live-stack.md).
- A kernel source tree at `KDIVE_KERNEL_SRC` (default `~/src/linux`).
- For the **kdump capture leg only**: the worker venv must `import guestfs, drgn`. The
  preflight (`scripts/check-local-libvirt.sh`) detects the gap and prints the one-time fix;
  see the [four-method runbook §4b](../../docs/operating/runbooks/four-method-live-run.md#wire-the-worker-venv-drgn--libguestfs).

`up.sh` runs the preflight first and stops with an actionable message if anything is
missing.

## Files

| File | Purpose |
|------|---------|
| `env.sh` | Sources the live-stack env, then sets `KDIVE_PROJECT`, `KDIVE_GUEST_IMAGE`, `KDIVE_LIBVIRT_URI=qemu:///system`, and `KDIVE_PYTHON`. Source it; don't run it. |
| `up.sh` | Idempotent bring-up: preflight → duplicate-trio guard → staging dir → backends → migrate → seed project → start the trio as root → block on `/readyz` → merge `.mcp.json`. |
| `down.sh` | Stop the root processes by pid file (`sudo kill`, then SIGKILL survivors); keeps the pid file if any process refuses to die. Backends are left running. |
| `mint-token.sh` | Print an admin developer token for `KDIVE_PROJECT` to stdout. |
| `mcp.json` | The MCP client config installed into the kernel tree; reads the token from `${KDIVE_TOKEN}` (holds no secret). |

## Usage

```bash
# 1. Bring everything up (prompts once for sudo; starts root processes on qemu:///system).
examples/local-libvirt/up.sh

# 2. In the shell you launch your MCP client from, export a fresh token:
export KDIVE_TOKEN=$(examples/local-libvirt/mint-token.sh)

# 3. Open your MCP client in the kernel tree — it reads the installed .mcp.json:
cd ~/src/linux            # the .mcp.json up.sh installed lives here
# ...launch your MCP client (it connects to http://127.0.0.1:8000/mcp as Bearer $KDIVE_TOKEN)

# 4. When finished, stop the processes (backends stay up):
examples/local-libvirt/down.sh
docker compose down -v    # from the repo root, to remove the backends + volumes
```

## Tokens

`mint-token.sh` mints a bearer token from the mock-OIDC issuer carrying
`roles={KDIVE_PROJECT: admin}` plus `platform_admin`/`platform_operator`.

- **Lifetime.** The token expires after `KDIVE_TOKEN_TTL` seconds. The default is `43200`
  (12h) — the mock issuer's own default is one hour, but this example overrides it because a
  build→boot→capture session routinely runs longer. The value must be a positive integer of
  seconds (minimum `1`); **no maximum is enforced** — `exp` is simply set to `now +
  KDIVE_TOKEN_TTL`, so you can mint a token that lasts as long as you like (a long-lived
  dev token is a mild security trade-off, acceptable only because the issuer is the bundled
  mock on your own machine). Set `KDIVE_TOKEN_TTL` before minting to change it.
- **Refreshing in a running session.** The installed `.mcp.json` carries
  `Authorization: Bearer ${KDIVE_TOKEN}`, and your MCP client expands `${KDIVE_TOKEN}` from
  its environment **once, when it connects** — it does not re-read the variable mid-session.
  So re-exporting `KDIVE_TOKEN` alone does nothing to a live connection. To pick up a new
  token (after expiry, or any time): re-run step 2 to export a fresh one, then **reconnect**
  the `kdive` server in your client (in Claude Code: `/mcp` → reconnect, or restart). Once a
  token expires, in-flight tool calls fail with `401` until you reconnect.

## How bring-up and teardown behave

- **Readiness gate.** `up.sh` does not print "stack is up" on a timer. After starting the
  trio it polls the server's `/readyz` and only prints success once it returns `200` (pg +
  MinIO + mock-OIDC all reachable). If a process exits first or readiness is not reached
  within 90s, `up.sh` prints the per-process log paths under `KDIVE_STACK_LOG_DIR` and exits
  non-zero. `/readyz` is served by the aux health listener on `127.0.0.1:9464` (the server's
  per-process default), **not** on the MCP port `:8000`, which has no health routes.
  - *Caveat:* if you export `KDIVE_HEALTH_BIND_ADDR`, it applies to **all three** processes,
    so worker and reconciler then collide with the server on that one port and fail to bind.
    Leave it unset (the default specializes the port per process: server `9464`, worker
    `9465`, reconciler `9466`). `up.sh` polls whatever `KDIVE_HEALTH_BIND_ADDR` resolves to,
    falling back to `127.0.0.1:9464`.
- **Duplicate-run guard.** `up.sh` refuses to start if a root `kdive` trio is already
  running (a second trio would fight over the same domains); stop the first with `down.sh`.
- **`.mcp.json` is merged, not clobbered.** If `KDIVE_KERNEL_SRC/.mcp.json` already exists,
  `up.sh` copies it to `.mcp.json.bak` and replaces only its `kdive` server entry — any other
  MCP servers you configured (and any other top-level keys) are preserved. A missing file is
  created from the template. The step is idempotent.
- **Teardown verifies.** `down.sh` sends `sudo kill` to each live pid, waits, escalates to
  SIGKILL for any that remain, and reports survivors. If a process cannot be stopped it keeps
  the pid file and exits non-zero so a re-run can finish the job; a clean stop removes it.

## Configuration

Everything is overridable from the environment before running the scripts:

| Variable | Default | Meaning |
|----------|---------|---------|
| `KDIVE_PROJECT` | `local` | Project the stack seeds and the token grants `admin` on. |
| `KDIVE_KERNEL_SRC` | `~/src/linux` | Kernel tree under test; where `.mcp.json` is installed. |
| `KDIVE_GUEST_IMAGE` | `…/fedora-kdive-ready-43.qcow2` | Catalog rootfs the System boots. |
| `KDIVE_LIBVIRT_URI` | `qemu:///system` | libvirt connection the worker drives. |
| `KDIVE_PYTHON` | `<repo>/.venv/bin/python` | Interpreter for `python -m kdive` and the processes. |
| `KDIVE_LIMIT_KCU` / `KDIVE_MAX_ALLOC` / `KDIVE_MAX_SYS` | `1000000` / `4` / `4` | Seeded budget and quota. |
| `KDIVE_TOKEN_TTL` | `43200` (12h) | Lifetime in seconds of the token `mint-token.sh` issues. Minimum `1`; no enforced maximum. |
| `KDIVE_STACK_PID_FILE` / `KDIVE_STACK_LOG_DIR` | `~/.local/state/kdive/local-stack.pid` / `…/local-stack-logs` | Where `up.sh` records the process pids and writes per-process logs. |

The pid file and logs live under the XDG state dir (`$XDG_STATE_HOME`, default
`~/.local/state/kdive`) — the same place the `kdive login` token cache lives — not inside
the repo. Set `XDG_STATE_HOME`, or the two `KDIVE_STACK_*` variables, to relocate them.

If you change `KDIVE_HTTP_HOST`/`KDIVE_HTTP_PORT` from `127.0.0.1:8000`, edit the `url` in
the installed `~/src/linux/.mcp.json` to match.

## Security notes

- The bundled mock-OIDC issuer mints a valid token for **any** caller. This example is for
  a developer's own machine only — never point `mint-token.sh` at a real deployment;
  production brings its own token via `$KDIVE_TOKEN`.
- `.mcp.json` references the token through `${KDIVE_TOKEN}` and never stores it, so the file
  is safe to leave in the kernel tree.
- `seed-demo` writes budget/quota with raw `INSERT`s and no audit row — a deliberate
  token-less bootstrap for a single-developer box. Onboarding someone else's tenant uses
  the audited admin tools instead; see
  [project onboarding](../../docs/operating/project-onboarding.md).
