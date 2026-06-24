# Expected-crash capture disclosure (#760)

- **Status:** Draft
- **Date:** 2026-06-24
- **Issue:** [#760](https://github.com/randomparity/kdive/issues/760) (Part 3 black-box review, epic #764)
- **ADR:** [ADR-0239](../../adr/0239-expected-crash-capture-disclosure.md)
- **Related:** [ADR-0233](../../adr/0233-live-attach-halted-early-boot-crash.md) (#747,
  `crashed_halted_live`), [ADR-0227](../../adr/0227-early-boot-console-crash-postmortem-guidance.md)
  (`postmortem.triage` console self-redirect), [ADR-0235](../../adr/0235-per-run-console-evidence.md)
  (per-Run console evidence).

## Problem

A System provisioned for post-crash inspection (`debug.preserve_on_crash`, `debug.gdbstub`,
`crashkernel`) that then hits a *declared* early-boot crash gets the `expected_crash_observed`
boot outcome. That outcome neither honors nor discloses the capture surface the System was
provisioned for:

1. The expected crash leaves the System `READY` (`jobs/handlers/runs_boot.py`), so every
   `CRASHED`-gated capture path — `vmcore.fetch`, `host_dump` (`mcp/tools/lifecycle/vmcore.py`
   requires `SystemState.CRASHED`) — rejects, even though provisioning advertised those methods.
2. `debug.start_session` is deliberately refused on `expected_crash_observed` and redirected to
   the console A/B flow (`mcp/tools/debug/sessions_lifecycle.py`, #759/ADR-0227), so `gdbstub`
   live-attach is also unavailable here.
3. The `expected_crash_observed` boot result carries **no** `available_capture` field, and
   nothing tells the agent that the `preserve_on_crash`/`gdbstub`/`crashkernel` flags it
   provisioned will not fire on this path.

The reviewer reported this as "flags are silently inert." The verified reframing: the flags are
real and wired (`preserve_on_crash` emits `<on_crash>preserve</on_crash>` via
`providers/local_libvirt/lifecycle/xml.py`); the gap is that the `expected_crash_observed`
outcome is **undiscoverable** — it does not tell the agent which capture mechanisms are and are
not reachable.

## Decision (direction)

**Disclose, do not re-route.** Keep `expected_crash_observed` as the console/postmortem A/B flow
that ADR-0227/#759 deliberately established; do **not** route an expected crash into the
live-attach `crashed_halted_live` state. Re-routing was the issue's alternative Option A; it is
rejected because ADR-0233/#759 intentionally separate a *declared* expected crash (→ console A/B
for reproduce/fix/re-verify) from an *unexpected* early panic with a reachable stub (→ live gdb
fallback). Re-routing would undo a just-merged decision.

Instead, make the outcome honest about its capture surface, at **boot time**, surfaced on
`runs.get`:

- The `expected_crash_observed` boot result records `available_capture` — the capture methods
  genuinely reachable on the `READY`-staying System after an expected crash — and `inert_capture`
  — the methods the System was provisioned for that will **not** fire on this path.
- `runs.get` surfaces both as `data.available_capture` / `data.inert_capture`, wiring a read path
  that `available_capture` (already recorded by `crashed_halted_live`, ADR-0233) never had.

## What is reachable on an expected crash

The System stays `READY`. Therefore:

| Method | Reachable on `expected_crash_observed`? | Why |
|--------|------|-----|
| `console` | **yes** | The per-Run console artifact (ADR-0235) is recorded as `evidence_artifact_id`; `postmortem.triage` self-redirects to it (ADR-0227). |
| `gdbstub` (live-attach) | no | `debug.start_session` is deliberately refused and redirected to the console A/B flow (#759/ADR-0227). |
| `host_dump` | no | `vmcore.fetch` requires `SystemState.CRASHED`; the System is `READY`. |
| `kdump` | no | Same `CRASHED` gate; an early-boot panic also precedes the kdump capture kernel. |

So `available_capture == ["console"]` for every `expected_crash_observed` outcome.

`inert_capture` is the provisioned-but-unreachable set, computed from the provider-neutral
`ProfilePolicy` predicates against the System's provisioning profile:

- `gdbstub` when `profile_policy.gdbstub_provisioned(profile)`
- `host_dump` when `profile_policy.host_dump_provisioned(profile)`
- `kdump` when `profile_policy.capture_method(profile) is CaptureMethod.KDUMP`

(All three predicates already exist on the `ProfilePolicy` protocol — no new port method.) The
order is deterministic (gdbstub, host_dump, kdump) for snapshot-stable output. A System
provisioned for none of these (console-only) yields `inert_capture == []`.

Using `ProfilePolicy` predicates — never a provider-specific profile section — keeps the generic
boot handler correct for local-libvirt, remote-libvirt, and fault-inject alike (the ADR-0233
discipline that the existing `_available_capture` already follows).

## Components and data flow

### 1. Boot handler — `jobs/handlers/runs_boot.py`

The `expected_crash_observed` branch in `_run_boot_and_capture_outcome` currently builds its
result inline. Extract a `_record_expected_crash(...)` helper that:

- fetches the System and parses its provisioning profile (mirroring `_record_crash_halted_live`);
- computes `available_capture = [CaptureMethod.CONSOLE.value]` and
  `inert_capture = _inert_capture(profile_policy, profile)`;
- records the boot audit and returns the result dict with the two new keys added to the existing
  `system_id`/`boot_outcome`/`expectation_matched`/`evidence_kind`/`evidence_artifact_id` fields.

If the System row is gone (`SYSTEMS.get` returns `None`) the helper degrades gracefully:
`available_capture == ["console"]`, `inert_capture == []` (the profile is unknowable). The outcome
is still recorded — losing the inert disclosure must not turn an expected crash into a hard
failure.

`_inert_capture(profile_policy, profile) -> list[str]` is a new pure helper beside
`_available_capture`.

### 2. Read model — `services/runs/steps.py`

`StepProgress` gains `available_capture: list[str] | None` and `inert_capture: list[str] | None`.
`step_progress` reads them from the `boot` step result (the same `Mapping` it already reads
`boot_outcome` / `evidence_artifact_id` from), coercing each to a `list[str]` (a non-list or a
list with non-string members yields `None`, fail-closed). Because the read is generic, a
`crashed_halted_live` outcome's existing `available_capture` is now surfaced too (it has no
`inert_capture`, so that key stays absent) — a consistency win, not a behavior change to ADR-0233.

### 3. Envelope — `mcp/tools/lifecycle/runs/common.py`

`envelope_for_run` adds `data.available_capture` and `data.inert_capture` on the `SUCCEEDED`
branch when `step_progress` carries them (omitted when `None`, like the other optional `data`
keys). `runs.get` advertises the generic envelope outputSchema (`data` free-form, #565), so the
new keys invalidate no committed snapshot and need no schema regeneration.

## Explicitly out of scope

- **Next-action wording.** `_succeeded_next_step` returns `["postmortem.triage", "vmcore.fetch"]`
  for `expected_crash_observed`; `vmcore.fetch` will reject (System `READY`). Aligning that
  next-action / the `debug.start_session` redirect is **#759**'s territory (a separate open
  sub-issue of the same epic). This change does not touch those surfaces, to avoid double-work
  and a merge conflict with #759.
- **Re-routing / preserving the guest.** The issue's "gap 4" (whether `<on_crash>preserve>`
  physically leaves the expected-crash guest paused) only matters for Option A. Under the chosen
  disclose-don't-re-route direction the expected-crash path relies on nothing being preserved, so
  no live preserve test is required for correctness.
- **A provision/`runs.create`-time advisory.** The acceptance is satisfied "at provision **or**
  boot"; boot-time disclosure satisfies both criteria for bound and unbound Runs alike without the
  `runs.create`/`runs.bind` double surface and the unbound-Run gap (the System and its capture
  flags are not known until a System is bound).

## Testing

Unit (`tests/jobs/handlers/test_runs_boot.py`), boundary-driven with injected fakes, mirroring
the existing `_record_crash_halted_live` / `_available_capture` tests:

- `_inert_capture` over each predicate combination: none (`[]`), gdbstub-only, host_dump-only,
  kdump-only (crashkernel), and all three (deterministic order).
- `_record_expected_crash` (or the `_run_boot_and_capture_outcome` expected branch) records
  `boot_outcome == "expected_crash_observed"`, `available_capture == ["console"]`, and the
  expected `inert_capture` for a capture-provisioned profile; one audit row.
- Degraded path: System gone → `available_capture == ["console"]`, `inert_capture == []`, outcome
  still recorded.

Read model (`tests/services/runs/test_steps.py`): `step_progress` surfaces both lists from a boot
result, coerces a malformed value to `None`, and leaves `inert_capture` absent for a
`crashed_halted_live` result while still surfacing its `available_capture`.

Envelope (`tests/mcp/tools/lifecycle/runs/`): a `SUCCEEDED` Run whose boot is
`expected_crash_observed` yields `data.available_capture == ["console"]` and the expected
`data.inert_capture`; a console-only profile yields `data.inert_capture == []`; the keys are
absent when the boot result carries neither.

No migration, no new tool, no request-shape or authz change. `live_vm` is not required: the
behavior is deterministic over the recorded boot result and the profile policy.
