# ADR 0329 — Probe runtime symbol resolution, not just the uploaded `.config`, for the drgn-live debuginfo warning

- **Status:** Accepted
- **Date:** 2026-07-12
- **Issue:** #1092
- **Spec:** [drgn-live missing_debuginfo](../archive/superpowers/specs/2026-07-09-introspect-missing-debuginfo-1064-design.md)
- **Builds on:** ADR-0322 (drgn-live `missing_debuginfo` warning), ADR-0240 (live drgn `run_script`)

## Context

ADR-0322 added a non-fatal `missing_debuginfo` warning so a blind drgn-live session is no longer
silently successful. That warning keys **entirely on the uploaded kernel `.config`**:
`debuginfo_warning` returns `None` (no warning) whenever `config.is_enabled("DEBUG_INFO_BTF")` is
true. The signal is static — it never asks whether the running guest's drgn could actually resolve
anything.

`BLACK_BOX_REVIEW.md` finding **F1** (verified, follow-up to #1064) is exactly the gap this leaves:
the reviewer's kernel had `DEBUG_INFO_BTF=y` in its `.config`, so the static gate returned no
warning — yet the in-guest drgn (0.0.31) could not load the kernel's BTF and every symbol lookup
failed. The precise scenario the warning was built for arrived **unwarned**, because the config
claimed BTF was present while the runtime could not use it. BTF presence in `.config` is necessary
but not sufficient for in-guest resolution: the guest image's drgn build, its BTF-loading support,
and kernel/BTF skew all sit between the advertised config and a working lookup, and none is visible
in the uploaded `.config`.

## Decision

We will add a **runtime resolution probe** to the drgn-live live-introspection path and combine it
with (never replace) the static config check. Before running the caller's helper/script on
`introspect.run` / `introspect.script`, we run a fixed one-line drgn probe over the existing
`run_script` seam — a bare lookup of the stable kernel global `init_task`. On a kernel whose
in-guest drgn cannot resolve symbols the lookup raises, the guest wrapper exits non-zero, and
`run_script` surfaces `DEBUG_ATTACH_FAILURE`; that is the blind-session signal the static gate
missed. When the probe proves resolution failed, the handlers emit a distinct
**`debuginfo_unloadable`** warning that names `DEBUG_INFO_BTF` and points at the remediations
(boot a BTF-capable guest image with a newer drgn, or upload a matching `vmlinux`).

The static config check stays first and authoritative:

- If the static check already warns (no BTF advertised, no uploaded `vmlinux`) — keep that
  `missing_debuginfo` warning; do not probe.
- If a host `vmlinux`/`debuginfo_ref` was uploaded — drgn resolves from it; do not probe.
- Only when the static check is **silent for a `vmlinux`-less Run** (BTF advertised, or no config
  uploaded) does a blind runtime remain possible. That, and only that, is when we probe.

The probe is **fail-open**: a probe that raises anything other than `DEBUG_ATTACH_FAILURE` (an
unreachable transport, a timeout) is indeterminate and adds no warning — the real introspection
call will surface that fault on its own. The probe never blocks or refuses an introspection.

The runtime warning rides the response `data` on both success **and** failure of the introspect
call: a blind session that makes the caller's helper exit non-zero returns an error, and the
`debuginfo_unloadable` cause + remediation is attached there too, so the agent learns *why* the
lookups failed instead of seeing an opaque attach failure.

The surface is `introspect.run` / `introspect.script` — the exact handlers F1 observed. The attach
seam (`debug.start_session(drgn-live)`) keeps only the cheap static early signal: its transport is
not yet open where the warning is computed, and the blind session manifests when symbols are
resolved (introspect), not at attach. Widening the runtime probe to attach is deferred as
unnecessary for closing F1.

## Consequences

- The F1 blind session — BTF advertised in `.config`, in-guest drgn unable to load it — now gets a
  loud `debuginfo_unloadable` warning naming the likely cause and remediations, on both the
  succeeding and failing introspect responses, instead of silence or an opaque attach error.
- The static `missing_debuginfo` warning and every other ADR-0322 path (uploaded `vmlinux`
  suppression, DWARF-only-config warning, fail-open on absent config) are unchanged; the runtime
  probe strictly *adds* a signal in the one gap the static check cannot cover.
- One extra in-guest `run_script` round-trip is paid per introspect call, but only in the narrow
  case that can still be blind: a `vmlinux`-less Run whose config advertises BTF (or has no
  uploaded config). Runs the static check already warns about, and Runs with an uploaded `vmlinux`,
  pay nothing new. This mirrors ADR-0322's accepted per-call fail-open read, one tier heavier for
  the sessions that look healthy by config but might not be.
- `debuginfo_unloadable` is a second reason code alongside `missing_debuginfo`; both carry the same
  `{reason, missing, remediation}` shape, so a client that keys on the payload structure is
  unaffected and one that keys on `reason` can distinguish "config never had BTF" from "runtime
  cannot load the BTF it advertised."
- The warning stays advisory and heuristic: `init_task` is a universally present global, so a false
  "unloadable" verdict is implausible, and warn-not-refuse means even one would never block a
  working session.

## Alternatives considered

- **Replace the static config check with the probe.** Loses the earliest, cheapest signal and the
  DWARF-only-config case that the static check already catches without a round-trip. The two
  signals are complementary; the probe fills a gap, it does not subsume the config check.
- **Parse the probe's stdout for a success sentinel.** Brittle and couples every provider's
  `run_script` (including the synthetic fault-inject double) to a magic marker. Using the
  seam's existing `DEBUG_ATTACH_FAILURE`-on-nonzero-exit contract needs no stdout parsing and is
  provider-agnostic — a healthy guest's probe simply returns, a blind guest's probe raises.
- **Probe at attach (`debug.start_session`) and cache the verdict on the session.** The transport
  is not open where the attach warning is computed, and persisting a verdict reintroduces the
  schema/staleness surface ADR-0322 explicitly rejected. Probing per introspect call keeps the
  signal fresh and stateless.
- **A new drgn helper subcommand (`kdive-drgn probe`).** Would change the operator-provided guest
  image contract for no gain: the existing `run-script` stdin mode already runs an arbitrary
  in-guest drgn script, which the fixed probe rides.
- **Refuse when the probe fails.** Same reason ADR-0322 warned rather than refused: a false verdict
  must be harmless, and a legitimate session must never be blocked by a heuristic.
