# drgn-live: surface `missing_debuginfo` when a session/introspect is blind (#1064)

- **Issue:** #1064 (P4 of the BLACK_BOX_REVIEW epic)
- **ADR:** [ADR-0322](../../adr/0322-drgn-live-missing-debuginfo-warning.md)
- **Type:** feature · **Scope:** medium
- **Status:** Draft

## Problem

`debug.start_session(drgn-live)` returns `live` and `introspect.run` / `introspect.script`
return `status: succeeded` even when every in-guest drgn symbol/type lookup fails because the
booted kernel carries no DWARF/BTF debuginfo (e.g. a defconfig kernel built without
`CONFIG_DEBUG_INFO_BTF`) and no host `vmlinux` was uploaded. The `ObjectNotFoundError`s land
inside the guest drgn process's captured stdout — the tool still reports success. Nothing warns
the agent that introspection is blind, so it reasonably concludes "drgn works, the data is just
absent."

Two code gaps (both CONFIRMED against source):

1. **No debuginfo signal at attach.** `debug.start_session` gates only on Run/System readiness,
   the endpoint being free, and (drgn-live) a per-System SSH bootstrap key. It never inspects the
   uploaded kernel config for `DEBUG_INFO*`/BTF and never checks for an uploaded `debuginfo_ref`.
2. **`introspect.*` success == "process ran," not "symbols resolved."** The live handlers return
   `succeeded` on any non-raising completion.

The primitives to fix this already exist (ADR-0318): a `debuginfo` feature in the
feature→`CONFIG_*` registry, `load_effective_config` (fail-open reader of the Run's uploaded
config), and the pure clause-support check. They are wired only for `crash_capture` today.

## Decision (summary — full rationale in ADR-0322)

Add a **non-fatal `missing_debuginfo` warning** to the drgn-live surfaces. Warn, never refuse, so
the DWARF-via-uploaded-`vmlinux` path keeps working. The warning is derived from a single shared
helper and surfaced in the response `data` (the same envelope shape the crash gate uses — no new
top-level `ToolResponse` field).

The warning fires when **all** of:

- the transport is `drgn-live` (the in-guest drgn path; gdbstub/offline-vmcore resolve symbols
  from the host-side uploaded `vmlinux` and are out of scope), and
- no host `vmlinux`/`debuginfo_ref` was uploaded for the Run (`run.debuginfo_ref is None`), and
- an `effective_config` was uploaded and it **provably** does not enable `CONFIG_DEBUG_INFO_BTF`.
  In-guest drgn reads BTF (`/sys/kernel/btf/vmlinux`); DWARF in the kernel `.config` does not help
  because the DWARF `vmlinux` is not on the guest rootfs, so the check keys on BTF specifically.
  Absent/unreadable/degenerate config → no warning (fail-open, matching the crash gate).

Surfaced on two seams:

- **`debug.start_session(drgn-live)`** — the earliest signal; the property is static for the
  session lifetime.
- **`introspect.run` / `introspect.script`** — closes the observed gap directly (introspect no
  longer looks unconditionally successful). `introspect.from_vmcore` is offline and always
  resolves via the uploaded `vmlinux` or reports `not_found`, so it is unchanged.

## Implementation

### `kernel_config/gate.py`
- Add `async def debuginfo_warning(conn, run_id, *, has_uploaded_vmlinux) -> dict | None`. Returns
  `None` when a `vmlinux` was uploaded, when the config is absent/unreadable/degenerate, or when
  the config enables `CONFIG_DEBUG_INFO_BTF`. Otherwise returns
  `{"reason": "missing_debuginfo", "missing": ["DEBUG_INFO_BTF"], "remediation": ...}`. Imports
  `load_effective_config` into the module namespace so tests patch it at
  `kdive.kernel_config.gate.load_effective_config` (matching the crash-gate tests). No change to
  `support.py`/`requirements.py`: the BTF check is a single-symbol lookup, not a clause set.

### `mcp/tools/debug/sessions_lifecycle.py`
- Compute the warning in `_prepare_attach_request` (has `conn` + `run`, runs **outside** the
  per-System advisory lock — no object-store I/O under the lock). Carry it on `_AttachRequest`.
  `_insert_session_locked` spreads it into the success `data` and, when present, prepends
  `artifacts.feature_config_requirements` to `suggested_next_actions`.

### `mcp/tools/debug/session_context.py` + `mcp/tools/debug/introspect.py`
- `resolve_debug_session_context(include_system=True)` already fetches the Run for `system_id`;
  also return its `debuginfo_ref` from the same fetch (new `DebugSessionContext.debuginfo_ref`).
  Thread `run_id` + `debuginfo_ref` onto `LiveDrgnSession`, compute the warning once in
  `_with_live_introspection`, and the two live handlers spread it into their `data` — no extra Run
  read per introspect call.

### Docs
- `docs/operating/external-build-upload.md`: reframe the DWARF/BTF note to "`CONFIG_DEBUG_INFO_BTF`
  is what in-guest `drgn-live` reads; DWARF needs an uploaded `vmlinux`," and cross-link
  `artifacts.feature_config_requirements`.

## Testing

- `tests/kernel_config/` gate test: `debuginfo_warning` — vmlinux suppresses; absent config →
  None; config with BTF → None; DWARF-only (no BTF) still warns; config lacking BTF → warning
  naming `DEBUG_INFO_BTF`.
- `tests/mcp/debug/test_debug_tools.py`: drgn-live attach surfaces `missing_debuginfo` when config
  lacks it; suppressed when config has BTF; suppressed when `debuginfo_ref` present; session is
  still `live` (non-fatal).
- `tests/mcp/debug/test_introspect.py`: `introspect.run`/`introspect.script` echo the warning and
  still report `succeeded`.

## Out of scope

- Gating/refusing (would break DWARF-via-`vmlinux`).
- gdbstub and `introspect.from_vmcore` (host-side symbol source).
- Parsing guest drgn stdout for `ObjectNotFoundError` (brittle; the config check is the signal).
