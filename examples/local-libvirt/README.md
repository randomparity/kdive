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
| `up.sh` | Idempotent bring-up: preflight → staging dir → backends → migrate → seed project → start the trio as root → install `.mcp.json`. |
| `down.sh` | Stop the root processes (`sudo kill` by pid file). Backends are left running. |
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

Tokens are short-lived; re-run step 2 and reconnect the client when one expires.

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
