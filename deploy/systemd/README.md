# KDIVE systemd units

The generic units cover server/reconciler host services. The generic worker units do not
arrange the mandatory authority-issued incarnation credential; use the fixed live-worker
contract below for that handoff. System-scope units are in [`system/`](system/); user-scope
units are in [`user/`](user/). Backends
(Postgres, S3, OIDC) are external and are not ordered by these units.

For prerequisites, installation, configuration, and logs, follow the
[systemd operating guide](../../docs/operating/systemd.md). It owns the system-scope and
user-scope procedures. This directory also ships the separate live-worker contract below.

## Fixed live-worker lifecycle contract

The live-VM workflows use the separate fixed-slot contract from ADR-0555 and ADR-0575. It
installs eight retained worker templates, a root socket-activated lifecycle witness, isolated
slot accounts, and a dedicated group-accessible session-libvirt endpoint. It does not convert
the server or reconciler into system units.

On a disposable hosted runner, install the contract after the checkout's `uv sync`. Supply the
lifecycle-witness member DSN on standard input so it is absent from the installer command line:

```bash
read -r -s witness_dsn
printf '%s\n' "$witness_dsn" | sudo env "PATH=$PATH" \
  deploy/systemd/install-live-worker-lifecycle.sh \
    --operator "$(id -un)" --source "$PWD"
unset witness_dsn
```

The installer is idempotent for one checkout on a fresh disposable host. It selects the host
distro's supported session daemon: Debian-family hosts use monolithic `libvirtd` and
`libvirt-sock`; Red Hat-family hosts use modular `virtqemud` and `virtqemud-sock`. An unsupported
distro, missing selected daemon, or missing distro `kvm` group fails before activation.
Persistent self-hosted runners receive the Debian-family tuple and the equivalent accounts,
files, modes, socket, witness environment, revision stamp, and session-libvirt resources from
the Ubuntu/Debian-only `live_vm_host` role.

The selected non-secret endpoint is published as `KDIVE_LIBVIRT_URI` in the root-owned,
world-readable `/etc/kdive/live-worker-libvirt.env`. The possible values are:

```text
qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock
qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock
```

Exactly one daemon tuple is activated; the installer does not create a compatibility socket alias.
`/run/kdive` is root-owned mode `0755`. Its `live-libvirt` and `live-libvirt/libvirt`
subdirectories are operator-owned mode `0750`, so workers can traverse to the explicit mode-`0770`
libvirt socket but cannot unlink or replace either control socket. Only provider data directories
are group-writable mode `2770`.

An existing endpoint is adopted only when its pid file names a live operator-owned process with
the selected daemon identity and its socket has the selected owner, group, mode, and a live
listener. A dead pid and refused, correctly owned selected socket are removed as exact stale
residues before restart. Contradictory process, type, ownership, or listener evidence is left
untouched; inspect the two paths named by the installer, correct that evidence, and rerun.
Before privileged inspection, the installer temporarily locks the runtime hierarchy as root,
rejects symlink or non-directory entries without following them, and restores operator ownership
on every exit. Stale removal rechecks file identity, process state, and the listener while locked;
any unlink or postcondition failure blocks startup and names the exact paths to inspect.

Only the configured operator belongs to `kdive-live-control`. Worker accounts keep distinct
primary groups and receive the `kdive-live-libvirt` and `kvm` supplemental groups; they never
belong to the control, sudo, or Docker groups. The distro `kvm` authority lets every worker read
`root:kvm` mode-`0640` host kernels for libguestfs and use `/dev/kvm` without making either
world-accessible. The witness credential and service configuration are root-only beneath
`/etc/kdive`; per-slot state is root-owned beneath `/var/lib/kdive/live-workers`, and each slot
account can neither traverse nor replace a sibling slot.

Adding the operator to `kdive-live-control` does not refresh an already-running process's kernel
group list. Interactive operators must start a new login session after installation before using
the installed socket.

## Lifecycle retry actions

Every response from `scripts/live-stack/worker-lifecycle.sh` carries a `retry_action` field
alongside its `code`. It is a closed set, and it — not the code — says what to do next.

| `retry_action` | What it means | What to do |
|---|---|---|
| `none` | the operation succeeded | nothing |
| `correct_request` | the request itself was rejected: a `start` missing its worker count or settings, or a request frame the witness could not parse | fix the invocation; retrying it unchanged fails the same way |
| `retry_same_operation` | a transient condition: the request deadline expired, termination evidence was rejected, or another lifecycle request holds the control lock (`code=busy`) | wait for the named condition to clear, then re-run the same command |
| `restore_systemd` | systemd could not answer for the retained unit | restore systemd, then re-run |
| `restore_database` | the database authority is unavailable | restore the database, then re-run |
| `operator_recovery` | any `conflict` response — retained lifecycle facts disagreeing with the observed unit, a worker incarnation disputed by an active fence, retained slot state that breaks a lifecycle rule, a systemd observation disagreeing with the retained lifecycle contract, or `recover` refusing a slot whose cgroup still holds live processes — an unmapped internal error, or a `diagnostics` capture that withheld at least one slot | inspect, then recover — see below |

`operator_recovery` is the one that never clears on its own: no retry of the same request will
change the outcome, because the retained facts and the observed unit disagree and something has to
resolve that disagreement. Run `scripts/live-stack/worker-lifecycle.sh status` and
`scripts/live-stack/worker-lifecycle.sh diagnostics` to find out which slot and why, then
`scripts/live-stack/worker-lifecycle.sh recover` to retire the slots proven dead. The procedure,
including how to read a withheld slot's reason, is in
[the live-stack runbook](../../docs/operating/runbooks/live-stack.md#recovering-a-wedged-worker-slot).

When `recover` itself is what returned `operator_recovery`, it refused a slot whose unit still has
live processes. The next step is to stop the work that unit is doing, not to re-run `recover`; the
runbook paragraph above says so in full.

`code=diagnostics_withheld` arrives with `operator_recovery`: `diagnostics` catches every per-slot
failure inside the capture and reports it as a withheld slot.
