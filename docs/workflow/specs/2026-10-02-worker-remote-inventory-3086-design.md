# Fixed live workers receive the remote-libvirt inventory (#3086)

Decision record: [ADR-0574](../../adr/0574-systemd-supervises-host-worker-incarnations.md),
amendment of 2026-10-02.

## Problem

The fixed live workers (`kdive-live-worker@N`) never see the operator's `systems.toml`. The gate
allowlist drops `KDIVE_SYSTEMS_TOML`, the slot accounts' home is `/nonexistent`, and the lifecycle
`start` settings carry no inventory path. The server and reconciler read the inventory, grant a
`remote-libvirt` allocation, and the worker then fails every remote provision and teardown with
`configuration_error: no provider runtime is registered for resource kind 'remote-libvirt'`.

## Design

1. **Contract.** `WorkerSettings` gains `systems_toml: str | None = None`, covered by the existing
   4-KiB validator and an absolute-path check. `SlotStore._environment` writes
   `KDIVE_SYSTEMS_TOML` only when the field is set; absent, `worker.env` is unchanged byte for byte.
   The lifecycle protocol identity hash moves; `LIFECYCLE_PROTOCOL_VERSION` stays 1.
2. **Gate.** `KDIVE_SYSTEMS_TOML` joins `_WORKER_ENV_NAMES`. Nothing else joins: not
   `KDIVE_SECRETS_ROOT`, `HOME`, or `XDG_CONFIG_HOME`.
3. **Witness trust check.** At the top of `SystemdWorkerLifecycle.start`, before the unmanaged
   worker scan and before any slot is stopped, prepared, or written, the witness checks the path
   with metadata only (`os.lstat` per component; it never opens, reads, copies, or parses the file):
   - the path is absolute and already normal (`os.path.normpath(path) == path`, so no `.`, `..`,
     or `//`);
   - no component is a symlink; every ancestor is a directory; the final component is a regular
     file;
   - for the file and each ancestor directory: the owner is not a slot account; group write is
     clear unless the group is outside the forbidden set; other-write is clear, except on a
     sticky-bit directory (which stops non-owners from replacing an entry).

   The forbidden users are `kdive-worker-1`..`8`. The forbidden groups are every group any slot
   account belongs to (`os.getgrouplist`) plus `kdive-live-libvirt`. A failed check returns
   `invalid_request` / `correct_request` with a fixed message that names the rule but echoes no
   metadata. The witness does not check readability. An unreadable inventory is not a loud
   failure in the worker: `load_inventory_optional` treats `EACCES` as "no file", so the worker
   would silently reproduce this issue. The launcher therefore checks readability before it sends
   the request (design 4).
4. **Launcher.** `scripts/live-stack/worker-lifecycle.sh start` resolves the inventory the way the
   operator's server and reconciler do (`systems_toml_path()`). It sends `systems_toml` only when
   `is_remote_libvirt_configured()` is true, so a local-only stack sends today's request. With a
   remote instance declared, it fails fast with exit 2 and a message naming the variable in three
   cases:
   - the resolved path is not absolute;
   - `secrets_root_from_env()` differs from the setting's default `/var/lib/kdive/secrets`, which
     is the root the workers always use;
   - the existing `require_worker_path_access` finds that some slot account cannot read the
     inventory (`r`) or traverse the secrets root (`x`). For example, the XDG default sits under a
     0700 or 0750 operator home.
5. **TLS ref layout (operator-provisioned, documented).** Remote client cert, key, and CA live
   under `/var/lib/kdive/secrets/remote-libvirt/`. The directory is `root:<group>` mode `0750`, the
   files are `root:<group>` mode `0440`, and `<group>` contains the slot accounts and the operator
   (`kdive-live-libvirt` on an installer-provisioned host). Refs read
   `remote-libvirt/clientcert.pem`. This follows the worker-authority TLS layout in the
   `live_vm_host` role. The secrets root itself must be root-owned and traversable by slot
   accounts: mode `0711`, as `worker_install_dirs.yml` provisions it. Ansible provisioning of the
   subdirectory is a follow-up under #2803.

## Failure model

1. **Actors and deployments.** The configured operator (the only `kdive-live-control` member)
   calls `start`. The root witness runs it. Slot UIDs `kdive-worker-N` run worker code, which may
   be compromised. Deployments: the host-systemd live stack (demo-up / `stack-services.sh`, native
   `live_vm`) with and without `[[remote_libvirt]]`. Helm and Compose are outside.
2. **Invariants and assets.** The root witness never reads operator-named content (confused
   deputy). A slot account cannot change the inventory another slot loads, because the file and its
   ancestors are unwritable by slot principals. Secret literals stay scrubbed in diagnostics. The
   worker confinement root stays fixed. `worker.env` is unchanged when no inventory is sent.
   - Delivery also makes fixed workers honor the inventory's other entries, for example
     `[[local_libvirt]] guest_egress`. Today they always see no inventory, which means
     restrict-on. Because delivery is gated on a remote block, local-only stacks keep today's
     behavior.
3. **Accepted failure classes.**
   - The operator swaps the file after `start` (time-of-check to time-of-use). The operator is
     trusted, and slot principals cannot write the path.
   - Write access granted by a POSIX ACL or another LSM rule is not detected. Only mode bits are
     checked; the operator owns ACL hygiene on their own file, as with every other operator path in
     `WorkerSettings`.
   - Whether the remote runtime is registered is decided at worker start. Adding or removing a
     `[[remote_libvirt]]` block needs a restart; hot-reload is excluded. Host entries are re-read
     on each operation, as on the server, so the operator edits them atomically (write, then
     rename).
   - A malformed inventory on the operator side reads as "no remote declared". Both server and
     worker then fail closed at operation time, as today.
4. **Covered elsewhere.** Cross-slot baseline and staging ownership: #3084. Remote deep-lifecycle
   cells: #2810. Helm and k8s secrets projection: #550. Ansible TLS subdirectory provisioning:
   follow-up under #2803.

### Threat model

- **Boundaries.** One boundary is added: an operator-supplied path crosses into the root witness,
  which runs `lstat` on it and writes it into the root-owned `worker.env`. One is widened: the gate
  passes one more variable to slot workers.
- **Actors.** Trust sits in the operator (peer-UID authenticated) and root. Slot workers are
  untrusted relative to one another and to root.
- **Controls.** The pydantic bounds (4 KiB, absolute) and the witness metadata check hold before
  any slot mutation. The existing newline and NUL guard in `_environment` holds. The gate's
  existing allowlist holds. Diagnostics classify by the existing
  `_SECRET_ENVIRONMENT_PATTERN`: `KDIVE_SYSTEMS_TOML` is public. A failed check leaks only the
  rule that failed.
- **Out of scope.** Inventory confidentiality from slot accounts, which must read it. A
  caller-selectable secrets root is rejected (ADR-0574 amendment).

## Success

- AC1: `start` with `systems_toml` → `worker.env` carries `KDIVE_SYSTEMS_TOML`, and the gate execs
  the worker with it.
- AC2: a relative, non-normal, symlinked, missing or non-regular, or slot-writable path is rejected
  `invalid_request` / `correct_request`. The fake stores and runtime record no call at all. A
  slot-writable path is one whose file or any ancestor is writable by a slot principal. The
  launcher separately refuses an inventory that a slot cannot read (design 4).
- AC3: without the field, `worker.env` equals today's rendering.
- AC4: `KDIVE_SECRETS_ROOT` is in neither the gate allowlist nor `worker.env`. A test pins the gate
  allowlist to exactly the `_environment` keys (all optional fields set), minus the
  gate-consumed or gate-pinned `KDIVE_WORKER_PYTHON`, `KDIVE_WORKER_INCARNATION_ID`, and
  `KDIVE_WORKER_INCARNATION_KIND`.
- AC5: the diagnostics secret-literal set excludes the `KDIVE_SYSTEMS_TOML` value.
- AC6: live proof on lab hosts. A remote provision through a fixed slot reaches `ready`. Release
  and teardown reach `torn_down` with no remote domain or volume left. The pre-existing stuck
  remote Systems tear down. The symlinked, slot-writable, and slot-unreadable arms are rejected at
  `start`. Omitting the field reproduces `configuration_error`.
- AC7: the remote runbook and `deploy/systemd/README.md` document the delivery and the TLS layout.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Field validation and env rendering (AC1, AC3) | focused-test | `tests/processes/lifecycle/systemd/test_systemd_worker_state.py` |
| Gate pass-through and allowlist parity (AC1, AC4) | focused-test | `tests/deploy/test_live_worker_gate.py` |
| Witness metadata check (AC2) | focused-test | `tests/processes/lifecycle/systemd/test_systemd_worker_inventory.py`, `test_systemd_worker_lifecycle.py` |
| Diagnostics classification (AC5) | focused-test | `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` (`load_slot_redaction_values`) |
| Launcher selection and fail-fast (design 4) | focused-test | `tests/scripts/test_live_stack_scripts.py` |
| Docs (AC7) | task-test-not-applicable | Prose for operators. The repo doc gates (`just ci` doc checks) are the only consumer. |
| Live arms (AC6) | task-test-not-applicable | Requires lab hosts. Results are recorded in the PR. |
