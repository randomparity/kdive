# Libvirt-free entry points survive a broken published contract (#2504)

## Problem

`lib.sh:66-68` and `env.sh:13-15` call the fail-closed `resolve_libvirt_uri` (#2480) at source
time, so a broken `/etc/kdive/live-worker-libvirt.env` aborts every live-stack entry point under
`set -euo pipefail` — recovery tools included, and `stack-down.sh:57`'s child spawn dies too.

## Scope

[ADR-0658](../../adr/0658-libvirt-free-entry-points-opt-out-of-resolution.md) keeps fail-closed
source-time resolution; an entry point declares itself libvirt-free by exporting
`LIBVIRT_OPTIONAL=1` before sourcing, which also reaches its children.
`scripts/live-stack/libvirt-uri.sh`, the resolution owner, gains a degraded
branch in `resolve_libvirt_uri` that, under the flag, leaves `KDIVE_LIBVIRT_URI` **unset** instead
of returning 1 and sets `LIBVIRT_UNRESOLVED` to one unexported line naming `$LIBVIRT_ENV`; the
diagnosis itself stays on stderr. Guarding that branch ahead of the load call keeps the second
source `stack-status.sh:9,11` performs silent. It adds `require_libvirt_uri <operation>`, refusing
an operation while unresolved. Neither new name takes a `KDIVE_` prefix; ADR-0658 records why.

`stack-down.sh` sets the flag and gates `--wipe` on that guard right after argument parsing,
before any teardown. `stack-status.sh` sets the flag and skips only its banner and probe at
`:55-60`; `provision_prereqs_ok` at `:61-65` reads no libvirt and keeps running. `lib.sh`, `env.sh`
and `worker-lifecycle.sh` are unedited — the last one's `stop` and `status` stop aborting under the
inherited flag, while `start` still fails closed through the flag-blind
`load_published_libvirt_uri`. The touched script and new tests cite ADR-0658.

### Failure model

- **Actors**: a local operator on a provisioned live-worker host, Red Hat and Debian families;
  the `live` CI job, which invokes `stack-down.sh` (`live.yml:423,676,817`).
- **Invariants**: server, reconciler and worker share one endpoint (#2480); `--wipe` never drops
  volumes while orphaning the domains it was asked to reap (`stack-down.sh:5-8`).
- **Accepted**: the flag exported into a libvirt-needing entry point, or an unguarded read added
  on a reachable path — the unset sentinel aborts each under `set -u`, as a `:-` default would not.
- **Covered elsewhere**: #2515, #2516, #2509. Unowned, not covered: `live.yml:673,815`
  pre-resolving before cleanup, and the three entry points ADR-0658 leaves unconverted.

## Success

1. On a broken-contract host, `stack-status.sh` exits 0, reporting its libvirt section unresolved
   and naming `$LIBVIRT_ENV`.
2. There, `stack-down.sh` completes plain teardown; `--wipe --yes` exits non-zero, stopping nothing.
3. Absent the flag, `resolve_libvirt_uri` still returns 1 on a broken contract.

## Validation

All but the last two are Mode: `focused-test` in `tests/scripts/test_live_stack_scripts.py`
against a staged broken `LIBVIRT_ENV`, via `just test-verbose <that path>`.

- Degraded branch, flag set: red while absent (sourcing exits non-zero); green when the URI is
  unset, `LIBVIRT_UNRESOLVED` names `$LIBVIRT_ENV`, and a re-source adds nothing.
- Fail-closed default, flag absent: non-zero exit, no fallback value.
- `require_libvirt_uri`: non-zero exit, names the refused operation on stderr.
- `stack-down.sh`: plain run exits 0, child inherits the flag; `--wipe --yes` exits non-zero
  reaching no teardown.
- `stack-status.sh`: exits 0, prints the unresolved banner, reaches no libvirt probe.
- Criterion 2's live proof — Mode: `focused-test`, manual arm: on a Red Hat-family and a
  Debian-family host, break `$LIBVIRT_ENV`, run both scripts before and after, restore it.
- ADR-0658 prose — Mode: `task-test-not-applicable`; no contract beyond the records gate.
