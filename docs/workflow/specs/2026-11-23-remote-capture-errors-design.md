# Remote capture failure boundaries

## Problem and authority

Required pre-delete silently succeeds on failure; DAC parsing discards the cause. At54f756e00,
`.desloppify/repro_remote_capture_errors.py` reproduces both (2 failures in0.35s).
Authority is the frozen eight-field `.desloppify/task-88-charter.md`.

## Scope and ownership

Extend `traffic_capture.py`, retaining its existing ownership and three deletion callers.
The helper translates libvirt errors with volume context and cause. Required pre-delete
propagates before volume creation/object-add; reclaim warns; rollback warns and preserves the
primary when libvirt cleanup fails. Keep absent volumes, pool guards and per-job names unchanged.
DAC parsing catches expected XML/defusedxml errors, chaining a configuration error with reason
`dac_xml_parse_failed`. Preserve parsed-label validation/remediation and netdev-before-DAC order.

## Success

Expected parser errors keep their causes; programming errors are not swallowed. Required delete
failure stops work; best-effort cleanup warns. Rollback preserves primary identity and cause,
including process-control errors, on libvirt deletion failure. Valid/absent controls stay valid.

## Failure model

- Actors and deployments: operator-configured remote-libvirt capture over existing TLS, worker
  capture and cleanup/reconciler callers; tests inject the external libvirt boundary on Linux.
- Invariants and assets: per-job clean retry; non-root QEMU ownership; original exception
  provenance; no unrelated volume deletion; no silent parser failure.
- Accepted failure classes: failed best-effort delete can leave a volume, with warning, because
  cleanup cannot override the primary operation; unexpected non-libvirt programming errors are
  surfaced, not generalized into operational categories. No promise of remote live/POWER proof.
- Covered elsewhere: TLS/secret lifetime by existing connection wrapper; pool permissions and
  ownership by ADR0432; exhausted-job orphan reclaim by ADR0556; stream setup by separate queue.

## Threat model

- Existing boundaries: host-provided domain XML into defusedxml; libvirt volume operation into
  worker error handling and operator logs. No boundary is added or widened.
- Actors: configured remote host can emit invalid XML or fail deletion; authenticated capture
  callers cannot choose a new host privilege through these changes.
- Controls: defusedxml stays in use; chain exceptions without embedding XML; use existing
  deterministic volume basename for details/logs; retain per-job deletion and pool guards.
- Out of scope: malicious host or administrator control, existing provider authorization and
  TLS compromise; those controls retain their existing owners and grants.

## Validation and constraints

The private plan supplies sequential parser/deletion tasks, focused inventories, faults, restoration,
regressions, original reproduction and wheel proof. Each task commits after its tests and hooks.
Bounded design review, scope audit and code/security reviews precede closure; campaign gates remain.

## Global Constraints

Python3.14 managed with uv; justfile guardrail recipes; no dependency or toolchain changes.
Declared x86_64 and ppc64le targets remain; host Linuxx86_64. Public contracts and applied
migration bytes remain unchanged. Local conventional commits only; no external writes.

## Decision

[ADR0847](../../adr/0847-preserve-remote-capture-failure-context.md) records the caller-owned
mandatory versus best-effort error policy. Existing provider ownership is extended, not moved.
