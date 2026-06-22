# Plan — A3: honest provider pointers on debug/introspect/host-dump (#673)

- **Issue:** #673 (Epic A, M2.8). **Spec:**
  [`../specs/2026-06-22-local-libvirt-service-parity-honesty.md`](../specs/2026-06-22-local-libvirt-service-parity-honesty.md) §A3.
  **ADR:** reuses [ADR-0175](../../adr/0175-partial-tool-maturity-reason.md). No new ADR/migration.
- **Type:** pure metadata; no behavior change.

## Problem

`debug.*`, `introspect.*`, and `vmcore.fetch`'s host-dump path are already `partial`, but their
`providers` pointer claims `local-libvirt: wired` (debug/introspect) or `local-libvirt:
HOST_DUMP/KDUMP` (vmcore.fetch). Local-libvirt's live-debug / introspection / host-dump seams are
`live_vm`-injected stubs that raise `MISSING_DEPENDENCY` in production — the half-truth this
milestone removes. Remote-libvirt implements all three.

`introspect.from_vmcore` carries no `providers` pointer at all.

## Decision

Rewrite the `providers` pointer on the in-scope tools to the spec wording:
`"local-libvirt: planned (M2.8 B*); remote-libvirt: implemented"`, with the fault-inject note
preserved where it is meaningful. Per-plane Epic-B reference:

- live-debug (`debug.*`, `introspect.run`) → B1/B3 (ADR-0210)
- offline introspection (`introspect.from_vmcore`) → B2 (ADR-0210)
- host-dump (`vmcore.fetch` HOST_DUMP) → B4 (ADR-0211); local KDUMP already implemented (#654)

## Tasks (single session, TDD)

1. **Failing drift guard first.** Add a test to `tests/mcp/core/test_tool_docs.py` asserting the
   in-scope tools carry a `providers` pointer that says local-libvirt is `planned` and
   remote-libvirt is `implemented` (and that `introspect.from_vmcore` now has one). Run it; confirm
   it fails for the expected reason (old "wired" wording / missing pointer).
2. **Update the pointers** in source:
   - `debug/ops.py::_gdbmi_maturity` (covers all 7 gdb-MI ops).
   - `debug/sessions.py` — `debug.start_session`, `debug.end_session`.
   - `debug/introspect.py` — `introspect.run` (rewrite) and `introspect.from_vmcore` (add).
   - `lifecycle/vmcore.py` — `vmcore.fetch`: local HOST_DUMP planned (B4), local KDUMP implemented;
     remote both implemented.
3. **Regenerate** `docs/guide/reference/*` via `just docs`; verify with `just docs-check`.
4. Guardrails: `just lint`, `just type`, `just test` (esp. `test_tool_docs`), `just docs-check`.

## Out of scope

Wiring any local seam (Epic B); the descriptor/admission work (A1 #672 / A2); any behavior change.

## Rollback

Pure metadata + generated docs; revert the commit.
