# Libvirt-free entry points survive a broken published contract (#2504)

## Problem

`lib.sh:66-68` and `env.sh:13-15` call the fail-closed `resolve_libvirt_uri` (#2480) at source
time, so a broken `/etc/kdive/live-worker-libvirt.env` aborts every live-stack entry point —
recovery tools included, and `stack-down.sh:79`'s child spawn dies too.

## Scope

[ADR-0660](../../adr/0660-libvirt-free-entry-points-opt-out-of-resolution.md) keeps fail-closed
source-time resolution; an entry point declares itself libvirt-free by exporting
`LIBVIRT_OPTIONAL=1` before sourcing, which also reaches its children.
`scripts/live-stack/libvirt-uri.sh`, the resolution owner, gains a degraded branch in
`resolve_libvirt_uri` that, under the flag, leaves `KDIVE_LIBVIRT_URI` **unset** instead of
returning 1 and records `LIBVIRT_UNRESOLVED` (unexported, naming `$LIBVIRT_ENV`; the diagnosis
stays on stderr). Guarded ahead of the load call, so the second source `stack-status.sh:14,16`
performs is silent. It adds `require_libvirt_uri <operation>`, refusing an operation while
unresolved. `stack-down.sh` sets the flag and gates `--wipe` on that guard right after argument
parsing, before any teardown; `stack-status.sh` sets it and skips only its banner and probe at
`:64-75`, keeping `provision_prereqs_ok`. `lib.sh`, `env.sh` and `worker-lifecycle.sh` are
unedited — the last one's `stop` and `status` stop aborting under the inherited flag, while
`start` still fails closed through `load_published_libvirt_uri`.

### Failure model

- **Actors**: a local operator on a provisioned live-worker host, Red Hat and Debian families;
  the `live` CI job (`live.yml:423,676,817`).
- **Invariants**: one shared endpoint (#2480); `--wipe` never drops volumes while the endpoint is
  unresolved. Behind a valid contract #2515 and #2516 still permit an orphaning reap.
- **Accepted**: the flag inherited by a libvirt-needing entry point, or an unguarded read on a
  reachable path. The unset sentinel aborts shell readers under `set -u` — but not
  `stack-services.sh --skip-libvirt`, which skips every such read and forks daemons that read
  `kdive.config`'s own `qemu:///system` default; that route is Unowned below.
- **Covered elsewhere**: #2515, #2516, #2509. Unowned: `live.yml:673,815` pre-resolving before
  cleanup; the three unconverted entry points, of which `--skip-libvirt` should also
  `unset LIBVIRT_OPTIONAL`; and `examples/local-libvirt/demo-down.sh`, which sources env first.

## Success

1. On a broken-contract host, `stack-status.sh` exits 0, reporting its libvirt section unresolved
   and naming `$LIBVIRT_ENV`.
2. There, `stack-down.sh` completes plain teardown; `--wipe --yes` exits non-zero, stopping nothing.
3. Absent the flag, `resolve_libvirt_uri` still returns 1 on a broken contract.

## Validation

All but the last two are Mode: `focused-test` in `tests/scripts/test_live_stack_scripts.py`.

- Degraded branch, flag set: red while absent; green when the URI is unset,
  `LIBVIRT_UNRESOLVED` names `$LIBVIRT_ENV`, and a re-source adds nothing.
- Fail-closed default, flag absent: non-zero exit, no fallback value.
- `LIBVIRT_UNRESOLVED` is an output and the current state: an inherited one does not suppress
  resolution; after a degrade an allowlisted `KDIVE_LIBVIRT_URI` clears it and is exported.
- `require_libvirt_uri`: non-zero exit, names the refused operation on stderr.
- `stack-down.sh`: plain run exits 0 with the child inheriting the flag; `--wipe --yes` exits 1.
- `stack-status.sh`: exits 0, prints the unresolved banner, reaches no libvirt probe.
- Criterion 2's live proof — manual arm: on a Red Hat-family and a Debian-family host, break
  `$LIBVIRT_ENV`, run both scripts before and after, restore it.
- ADR-0660 prose — Mode: `task-test-not-applicable`; no contract beyond the records gate.
