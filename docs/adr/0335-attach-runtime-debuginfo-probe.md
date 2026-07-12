# ADR 0335 — Extend the drgn-live runtime debuginfo-resolution probe to the `debug.start_session` attach seam

- **Status:** Accepted
- **Date:** 2026-07-12
- **Issue:** #1128
- **Builds on:** ADR-0329 (runtime resolution probe on `introspect.*`), ADR-0322 (drgn-live `missing_debuginfo` warning), ADR-0315/ADR-0289 (per-System bootstrap key seeding)

## Context

ADR-0329 added a runtime symbol-resolution probe so a blind drgn-live session — one whose uploaded
`.config` advertises `CONFIG_DEBUG_INFO_BTF` but whose in-guest drgn cannot actually load that BTF —
gets a loud `debuginfo_unloadable` warning instead of silence. It placed that probe **only** on the
`introspect.run` / `introspect.script` handlers, the exact seam `BLACK_BOX_REVIEW.md` F1 observed,
and explicitly deferred widening it to the attach seam `debug.start_session(drgn-live)`. The stated
reason: the attach warning is computed in `_prepare_attach_request` **before** the transport is
opened (the transport opens downstream in `_attach_debug_session`, between prepare and the locked
insert), and the blind session only *manifests* when symbols are resolved at introspect time.

The consequence is an ergonomic gap, not a correctness gap: an agent that attaches over a blind
guest sees a clean `live` session and only discovers the guest cannot resolve symbols on its first
`introspect` call. F1 is already closed at introspect; #1128 is the low-priority follow-up to move
the signal one step earlier so the caller learns at attach.

## Decision

Extend the ADR-0329 runtime probe to the attach seam, computed **after the attach transport is
open** and folded into the same `missing_debuginfo` warning the static config check produces. A
drgn-live `debug.start_session` on a blind guest now carries a `debuginfo_unloadable` warning in the
attach response `data`, before any `introspect` call, while still returning `live`.

Two options were on the table for the transport-not-open constraint ADR-0329 flagged:

1. **Open a dedicated probe transport during attach.** Rejected: `debug.start_session` already opens
   exactly the transport the session will use; opening a second one to probe doubles the attach-time
   IO and the failure surface for no benefit.
2. **Recompute the warning after the existing transport opens.** Chosen. `_attach_debug_session`
   already opens the transport between the prepare step and the locked insert; the probe rides that
   just-opened handle over the existing ADR-0240 `run_script` seam — the same call the introspect
   probe uses — and the augmented warning is threaded into the `AttachRequest` before the insert, so
   it reaches `AttachAdmitted.missing_debuginfo` and the rendered response.

The static config check stays first and authoritative; the probe is confined to exactly the gap it
cannot cover, and the gating is shared with the introspect seam via one
`augment_with_runtime_probe` helper (extracted from `introspection/live.py` into
`introspection/gate.py`). The attach probe is built (`_runtime_probe`) **only** when: the transport
is `drgn-live`, the static check is silent, and the Run uploaded no host `vmlinux`. A gdbstub
attach, a Run the static check already warns about, and a Run with an uploaded `vmlinux` pay nothing
new — no key load, no round-trip.

The probe runs **outside** the per-System lock (like `_open_transport`) and is fully **fail-open**:

- The runtime probe itself mirrors `probe_symbol_resolution` — a `DEBUG_ATTACH_FAILURE` is the
  blind-session signal (`debuginfo_unloadable`), any other fault (unreachable transport, timeout) is
  indeterminate and adds no warning.
- The per-System bootstrap key the probe needs is loaded fresh and fail-open: a drgn-live
  realization that does not ride the loopback SSH forward (guest-agent) seeds no key, so its absence
  is expected — the probe is skipped and the static warning stands. A missing key never blocks the
  attach.

The attach still succeeds in every case; the probe strictly *adds* a warning in the narrow
static-silent, `vmlinux`-less, drgn-live case.

## Consequences

- A blind drgn-live guest is now flagged at `debug.start_session`, not only at the first
  `introspect` — the attach response's `data.missing_debuginfo` carries `debuginfo_unloadable` and
  `suggested_next_actions` leads with `artifacts.feature_config_requirements`.
- One extra in-guest `run_script` round-trip is paid per attach, but only for a drgn-live session
  that looks healthy by config yet uploaded no `vmlinux` — the same narrow gap ADR-0329 already pays
  for at introspect. gdbstub, statically-warned, and `vmlinux`-carrying attaches are unchanged.
- The runtime probe now has one source of truth (`introspection/gate.py`) shared by the attach and
  introspect seams, so their gating and fail-open semantics cannot drift. No new reason code (the
  `debuginfo_unloadable` payload is unchanged) and no schema/DB change.
- The probe verdict is recomputed per attach and never persisted, keeping the signal fresh and
  stateless — the same reason ADR-0329 rejected caching the verdict on the session row.

## Alternatives considered

- **Open a separate probe transport at attach.** Doubles attach-time IO and failure surface; the
  session's own transport is already open at the augmentation point. Rejected.
- **Persist the attach probe verdict on the session.** Reintroduces the schema/staleness surface
  ADR-0322 and ADR-0329 both rejected; the introspect seam already re-probes per call. Rejected.
- **Leave the probe at introspect only (status quo).** The deferred state; #1128 is precisely the
  request to close the ergonomic gap, and the transport-not-open obstacle dissolves once the probe
  is computed after `_open_transport` rather than in `_prepare_attach_request`.
- **Refuse the attach when the probe fails.** Same reasoning as ADR-0322/ADR-0329: a heuristic must
  warn, never block a legitimate session.
