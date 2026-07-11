# ADR 0239 — Disclose the capture surface of an expected early-boot crash (#760)

- **Status:** Accepted
- **Date:** 2026-06-24
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0233](0233-live-attach-halted-early-boot-crash.md)
  (the `crashed_halted_live` outcome + `available_capture`), [ADR-0227](0227-early-boot-console-crash-postmortem-guidance.md)
  (`postmortem.triage` self-redirects an expected console crash to the console),
  [ADR-0064](0064-expected-boot-failures-artifact-search.md) (the declared expected-crash A/B
  flow), [ADR-0235](0235-per-run-console-evidence.md) (the per-Run console `evidence_artifact_id`),
  [ADR-0049](0049-crash-capture-tiers.md) (the capture-method vocabulary + `preserve_on_crash`/
  `gdbstub` flags).
- **Issue:** [#760](https://github.com/randomparity/kdive/issues/760) (part of #764).
- **Spec:** [`../superpowers/specs/2026-06-24-expected-crash-capture-disclosure-design.md`](../archive/superpowers/specs/2026-06-24-expected-crash-capture-disclosure-design.md).

## Context

A System provisioned for post-crash inspection (`debug.preserve_on_crash`, `debug.gdbstub`,
`crashkernel`) that hits a *declared* early-boot crash gets the `expected_crash_observed` boot
outcome. The expected crash leaves the System `READY` (so the `CRASHED`-gated `vmcore.fetch` /
`host_dump` reject), and `debug.start_session` is deliberately redirected to the console A/B flow
(#759/ADR-0227, ADR-0064), so `gdbstub` live-attach is unavailable too. Yet the
`expected_crash_observed` boot result carries no `available_capture`, and nothing tells the agent
that the `preserve_on_crash`/`gdbstub`/`crashkernel` flags it provisioned will not fire on this
path. The reviewer reported "flags are silently inert."

The reframing (verified against `main`): the flags are real and wired — `preserve_on_crash` emits
`<on_crash>preserve</on_crash>` (`providers/local_libvirt/lifecycle/xml.py`). The genuine defect
is that the outcome is **undiscoverable**: it neither honors nor discloses the capture surface the
System was provisioned for.

`available_capture` already exists on the ADR-0233 `crashed_halted_live` result but is recorded
only — `runs.get` never surfaces it (`StepProgress` reads only `boot_outcome` and
`evidence_artifact_id`).

## Decision

Disclose, do not re-route. Keep `expected_crash_observed` as the console/postmortem A/B flow;
make it honest about its capture surface at boot time, surfaced on `runs.get`.

1. The `expected_crash_observed` boot result records:
   - `available_capture = ["console"]` — the only method reachable on the `READY`-staying System
     (live-attach is refused per #759/ADR-0227; `host_dump`/`kdump` need `CRASHED`).
   - `inert_capture` — the provisioned-but-unreachable methods, computed from the provider-neutral
     `ProfilePolicy` predicates: `gdbstub` (`gdbstub_provisioned`), `host_dump`
     (`host_dump_provisioned`), `kdump` (`capture_method is CaptureMethod.KDUMP`), in that
     deterministic order. A console-only profile yields `[]`.
2. `services/runs/steps.py` `StepProgress` reads `available_capture` / `inert_capture` from the
   boot step result (fail-closed to `None` on a malformed value), and `runs.get`
   (`mcp/tools/lifecycle/runs/common.py`) surfaces them as `data.available_capture` /
   `data.inert_capture` when present. The read is generic, so a `crashed_halted_live` outcome's
   `available_capture` is now surfaced too.

The boot result is the single source of truth (the profile is already loaded at boot). If the
System row is gone when the outcome is recorded, the outcome still records with
`available_capture = ["console"]` and `inert_capture = []` — the disclosure degrades, the outcome
does not fail. No new `ProfilePolicy` method, tool, request-shape, authz, or migration; `runs.get`
advertises the generic envelope outputSchema (`data` free-form, #565), so the new keys invalidate
no committed snapshot.

## Consequences

- An agent that provisions capture flags and declares an `expected_boot_failure` learns at boot,
  via `runs.get`, exactly which capture mechanisms are reachable (`available_capture`) and which
  provisioned flags will not fire here (`inert_capture`) — satisfying the issue's acceptance.
- The `expected_crash_observed` outcome's advertised capture availability now matches reality: no
  advertised-but-unreachable mechanism.
- `crashed_halted_live` gains a surfaced `available_capture` on `runs.get` at no extra cost — the
  read path it always lacked.
- The boot handler fetches the System profile on the expected-crash branch (it already did on the
  `crashed_halted_live` branch) — one extra `SYSTEMS.get` on that branch only.

## Considered & rejected

- **Re-route the expected crash into `crashed_halted_live`** (the issue's Option A): make an
  expected crash live-debuggable when a stub is reachable. Rejected — ADR-0233/#759 deliberately
  separate a *declared* expected crash (→ console A/B) from an *unexpected* early panic with a
  reachable stub (→ live gdb fallback); re-routing reverses a just-merged decision and conflates
  the two flows.
- **A `runs.create`-time advisory** when a Run combines `expected_boot_failure` with capture
  flags. Rejected as the primary mechanism — the capture flags live on the System's provisioning
  profile, not the Run, and a Run may be unbound at create (ADR-0169), so the System (and its
  flags) is unknown until bind; boot-time disclosure is the single point where Run + System +
  profile always coincide, and it satisfies the "provision **or** boot" acceptance.
- **Transition the System to `CRASHED` on an expected crash** to make `vmcore.fetch`/`host_dump`
  reachable. Rejected — the expected crash staying `READY` is the established A/B-flow contract
  (the System is reusable for the reproduce→fix→re-verify loop); flipping it to `CRASHED` is a
  larger state-machine change that contradicts ADR-0064/#759 and is unnecessary for disclosure.
- **Compute `available_capture` at read time** in `runs.get`. Rejected — `runs.get` does not load
  the provisioning profile, and the profile is already in hand at boot; recording at boot keeps a
  single source of truth.
- **A dedicated `kdump_provisioned` `ProfilePolicy` predicate.** Rejected — `capture_method(...)
  is CaptureMethod.KDUMP` already expresses it provider-neutrally; a new port method is surface
  for no gain (YAGNI).
