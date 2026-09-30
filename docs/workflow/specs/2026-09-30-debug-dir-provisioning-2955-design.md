# Debug transcript directory provisioning (#2955) — design

Decision record: dated amendment to [ADR-0088](../../adr/0088-deployment-packaging.md) decision 3.

## Problem

The MCP server writes gdb-MI debug transcripts to `KDIVE_DEBUG_DIR` (default
`/var/lib/kdive/debug`) through `_default_transcript_dir()` in
`src/kdive/mcp/tools/debug/operations/runtime.py`. No live-host provisioning step creates the
directory, and `/var/lib/kdive` is root-owned on a provisioned host. The first debug operation
fails with `[Errno 13] Permission denied`. The setting declares `processes=_WORKER`, but no
worker code reads it (`rg -n DEBUG_DIR src` finds only `runtime.py`).

## Design

1. `src/kdive/config/core_settings.py`: `DEBUG_DIR.processes` becomes `_SERVER`. The worker
   is removed from the scope because it does not read the setting. `just config-docs`
   regenerates `docs/guide/reference/config.md` in the same commit.
2. `scripts/live-stack/stack-services.sh`: after the libvirt block and before
   `restart_host_processes`, create `${KDIVE_DEBUG_DIR:-/var/lib/kdive/debug}`. The step uses
   the same rule as the provision directories: skip when the directory exists and the invoking
   user can write it, else `sudo install -d -o "$(id -un)" -m 0750`. The invoking user is the
   account that starts the host server. The step runs also under `--skip-libvirt`, because the
   server needs the directory in every services bring-up. A failed `sudo install -d` exits 1
   with a message that names the directory and the fix (re-run the `live_vm_host` play, or
   create the directory owned by the operator).
3. `deploy/ansible/roles/live_vm_host/tasks/main.yml`: one `ansible.builtin.file` task creates
   `/var/lib/kdive/debug` as a directory owned by `live_vm_host_operator_user` (user and
   group), mode `0750`, `follow: false`. The operator account starts the stack on a
   role-provisioned host and can lack passwordless sudo (#1293), so the role must create it.
4. ADR-0088 gets a dated amendment: the server writes debug transcripts to `KDIVE_DEBUG_DIR`;
   the worker does not read it. Helm/compose server volumes stay with the operator follow-up.

Mode `0750`: transcripts can hold guest register and memory values; only the owner writes and
no other account needs to read them.

## Failure model

1. **Actors and deployments** — the lifecycle-control operator who runs `stack-services.sh`;
   the Ansible `live_vm_host` play run by an administrator; the host MCP server process.
   Compose and Helm deployments are outside this model (operator follow-up).
2. **Invariants and assets at stake** — the server can write transcripts with no manual step;
   the generated config reference matches the registry (`config-docs-check`); no directory is
   made writable to an account other than the operator.
3. **Accepted failure classes** — a non-default `KDIVE_DEBUG_DIR` on a role-provisioned host is
   not created by the role (the role uses the default path, like the fixture paths); the
   stack-services step creates it when the operator has sudo. A pre-existing directory that the
   operator can write keeps its mode (same skip rule as the provision directories). A host
   provisioned before this change, with no debug directory and no passwordless sudo, now stops
   bring-up with the actionable message until the play is re-run; the first debug operation
   failed on that host before this change. Rollout: re-run the `live_vm_host` play on the
   self-hosted live runners (`.github/workflows/live.yml` native jobs) that lack passwordless sudo.
4. **Covered elsewhere** — Helm/compose volumes and the debug/crash directory layout (operator,
   ADR-0088 follow-up); moving the transcript writer to the worker (operator).

### Threat model

- **Boundaries** — none added. Widened: `sudo install -d` on an operator-supplied path
  (`KDIVE_DEBUG_DIR`, from the operator's own environment).
- **Actors** — the local operator only; the path comes from the operator's environment, the
  same trust as `KDIVE_INSTALL_STAGING` in the existing loop.
- **Controls** — the path is quoted; `install -d` sets owner and mode; the Ansible task uses
  `follow: false` so it does not follow a symlink.
- **Out of scope** — an operator who sets the path to a sensitive location already holds sudo.

## Success

1. `DEBUG_DIR.processes == frozenset({"server"})` and `just config-docs-check` passes.
2. A services bring-up with `--skip-libvirt` and an absent, unwritable debug directory runs
   `sudo install -d -o <user> -m 0750 <dir>`; with a writable directory it runs no sudo.
3. The role task creates the default directory with the stated owner, mode, and `follow: false`.
4. Live: on a native ppc64le KVM-HV host, the directory is absent before bring-up, the changed
   `stack-services.sh` creates it with owner operator and mode `0750`, and a live spine attach
   phase (`debug.read_registers`) passes.
