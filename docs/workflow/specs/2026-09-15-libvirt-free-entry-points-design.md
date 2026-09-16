# Libvirt-free entry points survive a broken published contract (#2504)

## Problem

`lib.sh:66-68` and `env.sh:13-15` call the fail-closed `resolve_libvirt_uri` (#2480) at source
time, so a `/etc/kdive/live-worker-libvirt.env` that fails validation aborts every live-stack entry
point under `set -euo pipefail` — the recovery tools included. It crosses a process boundary:
`stack-down.sh:57` spawns `worker-lifecycle.sh stop`, which sources both files and dies first.

## Scope

[ADR-0658](../../adr/0658-libvirt-free-entry-points-opt-out-of-resolution.md) records the
decision: keep fail-closed source-time resolution; an entry point declares itself libvirt-free by
exporting `KDIVE_LIBVIRT_OPTIONAL=1` before sourcing, which also reaches its children.

`scripts/live-stack/libvirt-uri.sh` (current and intended owner of resolution) gains a degraded
branch in `resolve_libvirt_uri` that, under the flag, leaves `KDIVE_LIBVIRT_URI` **unset** instead
of returning 1 and sets `KDIVE_LIBVIRT_UNRESOLVED` to one unexported line naming `$LIBVIRT_ENV`
(the diagnosis stays on stderr, where `load_published_libvirt_uri` writes it). Guarding the branch
on that variable, ahead of the load call, keeps the second source `stack-status.sh:9,11` performs
silent. It adds `require_libvirt_uri <operation>`, refusing an operation while unresolved.

`stack-down.sh` sets the flag and gates `--wipe` on that guard immediately after argument parsing,
before any teardown. `stack-status.sh` sets the flag and skips its whole libvirt probe block —
`libvirt_ok` reads the variable unguarded at `lib.sh:380` — reporting it unresolved instead.
`lib.sh`, `env.sh` and `worker-lifecycle.sh` are unedited; the last one's `stop` and `status` stop
aborting under the inherited flag, while `start` still fails closed through the flag-blind
`load_published_libvirt_uri`.

### Failure model

- **Actors**: a local operator on a provisioned live-worker host, Red Hat and Debian families;
  the `live` CI job, which invokes `stack-down.sh` at `live.yml:423,676,817`.
- **Invariants**: server, reconciler and worker share one endpoint (#2480); `--wipe` never drops
  volumes while orphaning the domains it was asked to reap (`stack-down.sh:5-8`).
- **Accepted**: the flag exported into a libvirt-needing entry point, or an unguarded read added
  on a reachable path — the unset sentinel aborts each under `set -u`, as a `:-` default would not.
- **Covered elsewhere**: #2515, #2516, #2509; `live.yml:673` pre-resolving before its cleanup.

## Success

1. On a broken-contract host, `stack-status.sh` exits 0, reporting its libvirt section unresolved
   and naming `$LIBVIRT_ENV`.
2. There, `stack-down.sh` completes plain teardown; `--wipe --yes` exits non-zero, stopping nothing.
3. Absent the flag, `resolve_libvirt_uri` still returns 1 on a broken contract.

## Validation

All but the last are Mode: `focused-test` in `tests/scripts/test_live_stack_scripts.py` against a
staged broken `LIBVIRT_ENV`, run by `just test-verbose <that path>`.

- Degraded branch, flag set: red while absent (sourcing exits non-zero); green when the URI is
  unset, `KDIVE_LIBVIRT_UNRESOLVED` names `$LIBVIRT_ENV`, and a second source adds nothing.
- Fail-closed default, flag absent: non-zero exit, no fallback value.
- `require_libvirt_uri`: non-zero exit, names the refused operation on stderr.
- `stack-down.sh`: plain run exits 0, child inherits the flag; `--wipe --yes` exits non-zero
  reaching no teardown.
- `stack-status.sh`: exits 0, prints the unresolved banner, reaches no libvirt probe.
- ADR-0658 prose — Mode: `task-test-not-applicable`; a decision record carries no executable or
  structural contract beyond `adr-status-check` and the records gate, which already run it.
