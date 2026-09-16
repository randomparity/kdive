# Libvirt-free entry points survive a broken published contract (#2504)

## Problem

`lib.sh:66-68` and `env.sh:13-15` call the fail-closed `resolve_libvirt_uri` (#2480) at source
time, so on a host whose `/etc/kdive/live-worker-libvirt.env` fails validation every live-stack
entry point aborts under `set -euo pipefail` before running — including `stack-down.sh` and
`stack-status.sh`, the recovery tools an operator reaches for *because* the host is broken.

## Scope

[ADR-0658](../../adr/0658-libvirt-free-entry-points-opt-out-of-resolution.md) records the
decision: keep fail-closed source-time resolution, and let an entry point declare itself
libvirt-free with `KDIVE_LIBVIRT_OPTIONAL=1` before sourcing.

`scripts/live-stack/libvirt-uri.sh` (current and intended owner of endpoint resolution) gains a
degraded branch in `resolve_libvirt_uri` that, under the flag, records `KDIVE_LIBVIRT_UNRESOLVED`
and leaves `KDIVE_LIBVIRT_URI` **unset** instead of returning 1 — sticky, so a second source
re-prints nothing — plus `require_libvirt_uri <operation>`, refusing an operation while the
endpoint is unresolved. `stack-down.sh` sets the flag and gates `--wipe` on that guard immediately
after argument parsing, before any teardown; `stack-status.sh` sets the flag and reports the
endpoint as unresolved instead of probing it.

Unchanged: `lib.sh`, `env.sh`, `worker-lifecycle.sh`, and every entry point that touches libvirt —
no caller migration, no obsolete path, byte-identical behaviour absent the flag. Deferred and
owned elsewhere: `--wipe` reaping nothing against a wrong daemon (#2515); `sudo virsh` at a
session socket failing silently (#2516); the preset-contradicts-contract guard (#2509).

### Failure model

- **Actors and deployments**: a local operator on a provisioned live-worker host, Red Hat and
  Debian families; the `live` CI job, which never sets the flag.
- **Invariants at stake**: server, reconciler and lifecycle worker share one endpoint (#2480);
  `--wipe` never reports success having reaped nothing it was asked to reap.
- **Accepted failure classes**: an operator exporting `KDIVE_LIBVIRT_OPTIONAL=1` into an entry
  point that needs libvirt, and an unguarded `$KDIVE_LIBVIRT_URI` read added later inside an
  opted-out one — both bounded: the unset sentinel aborts the consumer under `set -u`.
- **Covered elsewhere**: #2515, #2516, #2509 as listed above.

## Success

1. On a host with a broken published contract, `stack-status.sh` exits 0, printing its libvirt
   section as unresolved and naming the reason.
2. There, `stack-down.sh` completes plain teardown; `--wipe --yes` exits non-zero, stopping nothing.
3. Absent `KDIVE_LIBVIRT_OPTIONAL`, `resolve_libvirt_uri` still returns 1 on a broken contract.

## Validation

Every entry below except the last is Mode: `focused-test` in
`tests/scripts/test_live_stack_scripts.py` against a staged broken `LIBVIRT_ENV`, run by
`just test-verbose tests/scripts/test_live_stack_scripts.py`.

- Degraded branch, flag set: red while the branch is absent (sourcing exits non-zero); green when
  `KDIVE_LIBVIRT_URI` is unset and `KDIVE_LIBVIRT_UNRESOLVED` is non-empty.
- Fail-closed default, flag absent: non-zero exit, no fallback value.
- `require_libvirt_uri`: non-zero exit, names the refused operation on stderr.
- `stack-down.sh`: the `require_libvirt_uri` gate precedes the first teardown statement.
- `stack-status.sh`: guarded read plus the unresolved banner.
- ADR-0658 prose — Mode: `task-test-not-applicable`; a decision record carries no executable or
  structural contract beyond `adr-status-check` and the records gate, which already run it.
