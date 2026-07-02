# ADR 0292 — Fail diagnostic_sysrq when the guest rejected the SysRq

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** kdive maintainers

## Context

`control.diagnostic_sysrq` (ADR-0285) injects one allowlisted magic-SysRq keystroke into a
ready local-libvirt guest and stores the console dump the kernel prints. The handler
(`jobs/handlers/diagnostic_sysrq.py`) treats the job as succeeded whenever the console grew
past the pre-injection mark, and fails only when the capture returns `exit_reason ==
"no_output"` (the console did not grow at all).

When `kernel.sysrq` restricts the requested operation, the kernel still writes a line to the
console — `sysrq: This sysrq operation is disabled.` (verbatim `pr_info("This sysrq operation
is disabled.\n")` under the `sysrq: ` `pr_fmt` prefix in `drivers/tty/sysrq.c`; stable across
5.x–6.x). The console therefore grows, the capture ends `stabilized`/`hit_bound`, and the
handler stores that delta and returns `succeeded`. An agent trusting the `succeeded` envelope
gets a "task-state dump" with no task states — a silent wrong result (#952, observed on
`debian-kdive-ready-13`). The wrapper docstring already promises this case is a
`configuration_error`, so behavior contradicts the documented contract.

## Decision

Detect the disabled marker and fail the job, mirroring the existing `no_console_output`
remediation.

- The capture core (`capture_console_delta`) scans **only the post-injection growth**
  (`body[mark:]`, the bytes written after the pre-injection mark) for the substring
  `This sysrq operation is disabled.`. Scanning the post-mark slice — not the seam-overlap
  slice returned for storage — keeps an unrelated older marker in the retained boot log from
  triggering a false failure. On a match it returns a new `exit_reason == "disabled"`.
- The handler maps `exit_reason == "disabled"` to a `CategorizedError`
  (`CONFIGURATION_ERROR`, `reason="sysrq_disabled"`) with a remediation naming the fix
  (permit the operation in the guest's `kernel.sysrq` bitmask), a sibling of the
  `no_console_output` branch. No artifact is stored, so the job fails rather than persisting
  boot-console noise.
- Match on the distinctive substring rather than the full prefixed line: the `sysrq: ` prefix
  is synthesized from `KBUILD_MODNAME` at build time, and the substring is what is stable and
  unambiguous.

## Consequences

- A restricted `kernel.sysrq` now fails the job with an actionable category and reason, and
  stores nothing, matching the docstring contract.
- The per-kind job telemetry (ADR-0285) already surfaces a rising `configuration_error` rate;
  `sysrq_disabled` joins `no_console_output` and `control_failure` as a distinct reason under
  it.
- The success-path artifact is unchanged: it still starts at `mark - seam_overlap` so a
  secret straddling the capture start stays contiguous for redaction. Only the failure
  decision is new.

## Considered & rejected

- **Scope the stored capture to the post-injection delta (`body[mark:]`).** Suggested as
  optional in #952. Rejected here: the retained seam-overlap before `mark` is a deliberate
  redaction-safety property (a secret straddling the capture start must not leak its tail),
  and narrowing the success artifact is orthogonal to fixing the false success. Detection
  already reads the post-mark slice, so a disabled op stores nothing regardless.
- **Detect the marker in the handler on `result.raw`.** `result.raw` includes the
  seam-overlap bytes before `mark`, so a marker from an earlier op in the retained log could
  cause a false failure. Deciding in the capture core, against the true post-mark growth,
  avoids that.
- **Match the full `sysrq: This sysrq operation is disabled.` line.** The `sysrq: ` prefix is
  build-synthesized (`KBUILD_MODNAME`), so anchoring on the distinctive substring is more
  robust while still matching the exact line.
