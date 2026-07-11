# ADR 0322 — Warn (not refuse) when a drgn-live session/introspect is blind for lack of debuginfo

- **Status:** Accepted
- **Date:** 2026-07-09
- **Issue:** #1064
- **Spec:** [drgn-live missing_debuginfo](../archive/superpowers/specs/2026-07-09-introspect-missing-debuginfo-1064-design.md)
- **Builds on:** ADR-0318 (debug-feature config gate), ADR-0039 (drgn-live introspection)

## Context

`debug.start_session(drgn-live)` and the live `introspect.*` handlers report success on any
non-raising completion. When the booted kernel has no DWARF/BTF debuginfo and no host `vmlinux`
was uploaded, in-guest drgn resolves no symbols — but the failures land in the guest drgn
process's captured stdout, so the tool still returns `live` / `succeeded`. An agent reasonably
concludes "drgn works, the data is just absent" (BLACK_BOX_REVIEW finding P4).

ADR-0318 already built the machinery to read a Run's uploaded kernel config:
`load_effective_config` (a fail-open reader) and a pure `.config` parser. It wired them for
`crash_capture` only.

The relevant symbol for **drgn-live specifically** is BTF. In-guest drgn resolves symbols from the
running kernel's BTF (`/sys/kernel/btf/vmlinux`, from `CONFIG_DEBUG_INFO_BTF`). DWARF built into the
kernel `.config` does **not** help: the DWARF-carrying `vmlinux` is not on the guest rootfs. The
only other in-guest source is a host `vmlinux` uploaded as the Run's `debuginfo_ref` (which drgn
can be pointed at). So a kernel with DWARF-in-config but no BTF and no uploaded `vmlinux` is still
blind — exactly the P4 case.

## Decision

We will surface a **non-fatal `missing_debuginfo` warning** on the drgn-live surfaces, and we
will **warn, not refuse**. A shared helper `debuginfo_warning(conn, run_id, *,
has_uploaded_vmlinux)` returns a `{reason, missing, remediation}` payload — or `None` — that both
seams spread into their response `data`, reusing the crash gate's envelope convention rather than
adding a new top-level `ToolResponse` field.

The warning fires only when all hold: the transport is `drgn-live`; no host
`vmlinux`/`debuginfo_ref` was uploaded for the Run; and an uploaded `effective_config` provably
does **not** enable `CONFIG_DEBUG_INFO_BTF`. It is surfaced at `debug.start_session(drgn-live)`
(earliest signal; the property is static for the session) and on `introspect.run` /
`introspect.script` (the exact handlers the finding observed reporting `succeeded`). The Run's
`debuginfo_ref` is resolved alongside the session (no extra fetch per introspect call).

Keying on BTF rather than the broader DWARF-or-BTF `debuginfo` advertise manifest is deliberate:
warn-not-refuse means a false warning (an exotic in-guest DWARF setup) is harmless, while a missed
warning (silent blindness) is the failure the feature exists to close.

## Consequences

- An agent starting a drgn-live session or running live introspection against a debuginfo-less
  kernel now gets a loud, symbol-naming `missing_debuginfo` warning and a pointer to
  `artifacts.feature_config_requirements`, instead of silent apparent success.
- The DWARF-via-uploaded-`vmlinux` path is preserved: an uploaded `debuginfo_ref` suppresses the
  warning, and nothing is ever refused.
- kdive reads the `SENSITIVE` `effective_config` on the debug path too; only derived booleans and
  public `CONFIG_*` names leave the seam, never config bytes — same boundary as the crash gate.
- The introspect handlers pay one extra fail-open config read per call (the Run's `debuginfo_ref`
  is resolved with the session, so no extra Run fetch). This is negligible beside the SSH+drgn
  round-trip they already perform, and avoids a caching/staleness surface.
- The warning is advisory and heuristic: it keys on the uploaded config, which kdive does not
  verify against the booted kernel, so at worst it warns for an exotic in-guest-DWARF setup — it
  never blocks a working session.

## Alternatives considered

- **Refuse instead of warn.** Would break the legitimate DWARF-via-uploaded-`vmlinux` path and
  turn a benign advisory read into a hard failure; the finding explicitly recommends warn.
- **Add `debuginfo` to `gate_required` and reuse `unmet_clauses`.** Conflates "the gate refuses on
  this" with "warn about this"; would arm a refusal at the crash/install seams too. The warn path
  is a standalone BTF check that never touches the gate registry's refusal set.
- **Key the warning on the DWARF-or-BTF `debuginfo` advertise clause.** Would suppress the warning
  for a DWARF-only, BTF-less kernel that in-guest drgn cannot actually use — reintroducing the
  silent-blindness the feature exists to close.
- **Attach-only (skip introspect).** Simpler, but leaves the exact handlers the finding observed
  (`introspect.run`/`introspect.script`) still reporting unconditional `succeeded` for a session
  reused past its attach response. Warning both closes the observed gap.
- **A new typed `warnings` field on `ToolResponse`.** The envelope has `extra="forbid"` and is
  round-tripped by the compact-response middleware; a new top-level field is broader surface than
  the established `data`-payload convention the crash gate already uses.
- **Parse guest drgn stdout for `ObjectNotFoundError`.** Brittle and locale/version-dependent; the
  uploaded-config check is a stable, structural signal.
- **A new `debuginfo` object-store column / cached verdict.** Adds a migration and staleness
  surface; the per-call fail-open read is cheap.
