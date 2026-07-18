# ADR-0383: Evaluate expected_boot_failure after a ready boot; add a ubsan preset (#1267)

- Status: Accepted
- Date: 2026-07-17

## Context

`expected_boot_failure` (ADR-0064, ADR-0266) lets a Run declare "I expect this boot to
fail with signature X" — the regression-test path where reproducing a crash *is* success.
When the boot console shows the declared signature, the boot step records
`boot_outcome=expected_crash_observed` (ADR-0239, ADR-0260) instead of a failure.

Two gaps, both verified in source and observed live on buggy-kernel run `8c1dfa61`
(`BLACK_BOX_REVIEW.md` F-06):

1. **The readiness marker pre-empts expected-failure evaluation.** In
   `jobs/handlers/runs/boot.py`, `_run_boot_and_capture_outcome` calls `booter.boot(...)`.
   The `expected_boot_failure` matching (`boot_evidence.expected_crash_matched_line` →
   `record_expected_crash`) lives *entirely* inside `except CategorizedError` and only runs
   for `ErrorCategory.READINESS_FAILURE`. Readiness itself keys off `classify_console`
   (`providers/local_libvirt/lifecycle/boot/readiness.py`), which scopes its crash scan to
   the console region **before** the `kdive-ready` marker. So a kernel that prints the
   marker and only *then* emits a UBSAN splat + `Kernel panic - not syncing` reaches the
   marker, `boot()` returns, control falls through to the success path, and the step records
   `boot_outcome=ready` unconditionally — a **false pass** for a regression test. The real
   evidence (the panic) was in the captured `console-<run_id>` artifact but was only found
   via a manual `artifacts.find`.

2. **No `ubsan` preset.** `CRASH_SIGNATURE_PRESETS` covers `panic`/`oops`/`hung_task`; the
   `kind` Literal is `console_crash|oops|panic|hung_task`; the shared `_CRASH_SIGNATURE`
   matcher has no UBSAN token. A UBSAN `shift-out-of-bounds` splat cannot be named as an
   expected boot-failure mode, even though it is exactly the memory-safety signal a debug
   kernel is built to surface.

This is the deliberate design-point neighbour of #1237 / ADR-0373: that issue handled the
*general* livelock-after-ready case (a healthy-looking `boot_outcome=ready` guest that
livelocks later) with a read-side `data.liveness` signal on `runs.get` **rather than**
reclassifying `boot_outcome`, because reclassifying the common healthy-boot outcome from a
best-effort console heuristic would be unsafe. The scope here is narrower and the opposite
call is correct: only a Run that *explicitly declared* `expected_boot_failure` is affected,
and for that Run a post-marker match means the operator's stated expectation was met.

## Decision

**1. Add a `ubsan` crash-signature preset.**

- Extend the `ExpectedBootFailure.kind` Literal to
  `console_crash|oops|panic|hung_task|ubsan`.
- Add `CRASH_SIGNATURE_PRESETS["ubsan"] = "UBSAN:"` — the stable header prefix that every
  UBSAN report line carries (`UBSAN: shift-out-of-bounds in …`,
  `UBSAN: array-index-out-of-bounds in …`, `UBSAN: signed-integer-overflow …`), matched as
  a case-sensitive literal substring like the other presets.
- Add `UBSAN:` to the shared `_CRASH_SIGNATURE` matcher (readiness fail-fast +
  `watch_for_crash`). This is consistent with the sanitizer reports already in that
  matcher (`KASAN:`, `KFENCE:`): all three are non-fatal-by-default kernel sanitizer
  reports that KDIVE — a kernel-debugging platform — treats as crash signals, because a
  sanitizer firing during a debug boot is the reproduction, not benign noise. So a UBSAN
  splat that appears *before* the marker now fails readiness like a KASAN splat does.

**2. Evaluate `expected_boot_failure` on the ready path too.**

On the success path of `_run_boot_and_capture_outcome`, after the boot-window console is
captured, if the Run declared `expected_boot_failure` and the captured console (which
includes post-marker output) matches the declared signature, record
`expected_crash_observed` (via the existing `boot_evidence.record_expected_crash`) instead
of `ready`. A new `boot_evidence.evaluate_expected_failure_after_ready` helper holds the
gate; it returns `None` (leaving `ready` unchanged) when no expectation is declared, the
console is empty, or nothing matches. `record_expected_crash` already emits the boot audit,
so the ready-path audit is skipped on the downgrade — the outcome is audited exactly once.

Blast radius: a Run with no `expected_boot_failure` is never reclassified; the ready path is
byte-for-byte unchanged for it. Only a Run that declared an expectation whose signature
appears anywhere in its boot console — before or after the marker — is downgraded.

## Consequences

- A regression test using `expected_boot_failure={"kind":"panic"}` (or `"ubsan"`) for a bug
  that emits the splat *after* the readiness marker now records
  `expected_crash_observed` — a correct pass-as-expected-failure — instead of a false
  `ready`. The `ubsan` preset alone would not fix this: without change 2 the marker still
  wins. Both are needed.
- `expected_crash_observed` on the ready path reuses the same evidence + inert-capture
  disclosure (ADR-0239) as the failure path, so `runs.get` reads identically regardless of
  which path recorded it.
- A UBSAN splat *before* the readiness marker now fails readiness (via `_CRASH_SIGNATURE`),
  matching the existing KASAN/KFENCE treatment. A boot that emits only benign non-UBSAN
  output is unaffected.
- Detection remains bounded by console capture: an expected failure whose splat never
  reaches the captured console (e.g. emitted after the snapshot) is still classified
  `ready`. This is the same capture-timing bound the failure path already has, not a new
  gap; run `8c1dfa61` confirmed the panic lands in the captured artifact in practice.
- No schema, RBAC, or config change — `expected_boot_failure` is a JSON column and the new
  outcome value already exists.

## Rejected alternatives

- **Documentation-only** (state that `expected_boot_failure` is evaluated only before the
  marker). Leaves the false pass in place — the very failure mode a regression test exists
  to catch.
- **Add the `ubsan` preset without the ready-path evaluation.** The marker still wins, so a
  post-marker UBSAN panic stays `ready`; the preset would only ever fire when the splat
  precedes the marker.
- **Reclassify `boot_outcome` for *any* ready boot that later shows a fatal signature**
  (not gated on `expected_boot_failure`). This is the call ADR-0373 deliberately rejected
  for the general case; widening reclassification to every Run would change the common
  healthy-boot outcome from a best-effort heuristic. Gating on an explicit declared
  expectation keeps the change safe.
- **A dedicated post-marker "crashed-after-ready" outcome.** Adds a new terminal state and
  state-machine edges for a case the existing `expected_crash_observed` already models
  correctly; unjustified surface for this fix.
