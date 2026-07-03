# ADR-0305: fast-fail `authorize_ssh_key` with a reachability pre-flight (#1012)

- Status: Accepted
- Date: 2026-07-02
- Builds on [ADR-0271](0271-system-direct-ssh-access.md) (`authorize_ssh_key`, the direct-SSH
  handler), [ADR-0289](0289-per-system-ssh-bootstrap-key.md) (the per-System bootstrap key and the
  bounded connect-retry it motivated), [ADR-0298](0298-ssh-reachable-runtime-probe.md) (the
  `check_ssh_reachable` banner probe reused as the pre-flight), and
  [ADR-0302](0302-ssh-failure-reason-classification.md) (the closed `SshFailureReason` vocabulary
  the pre-flight names its reason with). Sibling of the layered-verdict work
  ([ADR-0303](0303-layer-the-ssh-reachable-verdict.md), #1011).

## Context

`systems.authorize_ssh_key` did **no** reachability pre-flight. On an unreachable guest — a broken
image, an sshd that never started, a forward that never bridged — the handler went straight into
`run_ssh_with_retry` and burned a long retry window before failing. Throughout that window every
`jobs.get`/`jobs.wait` returned a bare `status:running` with no diagnostic content, so an agent
could not tell a doomed job from a slow one.

The observed wall-clock was **~230 s** against a `ready` guest with sshd stopped — well past the
`_AUTHORIZE_SSH_RETRY` policy's nominal `deadline_s = 90.0`. In the same window,
`systems.check_ssh_reachable` returned a definite `reachable:false, "no SSH banner"` verdict in
seconds.

**Root cause of the > 90 s overrun.** The 90 s deadline bounds a *single* call to
`run_ssh_with_retry`, and it does so correctly — but two facts stack:

1. The deadline gates when a *new* attempt may **start**, not when an in-flight one **completes**,
   so a single call's true ceiling is `deadline_s` plus one final `run_once` (up to the caller's
   own per-attempt subprocess timeout) — roughly 120 s, not 90 s.
2. The append failure the loop finally returns is raised as a **non-terminal** `TRANSPORT_FAILURE`.
   The worker requeues a non-terminal failure up to `max_attempts` (default `3`), running the whole
   ~90 s window **once per attempt**. Three requeues × ~76–90 s per window ≈ the ~230 s observed.

The retry budget was never bounding *total* (cross-attempt) wall-clock, because nothing told the
worker the doomed case is not worth another attempt.

## Decision

Pre-flight the recorded SSH endpoint with the existing reachability probe before entering the append
retry, and fail the doomed case terminally.

1. **Reuse the `check_ssh_reachable` probe as the pre-flight.** `authorize_ssh_key_handler` gains an
   injectable `probe: ProbeFn = _real_probe` (imported read-only from `ssh_reachable.py`, ADR-0298).
   After resolving `(host, port)` and before loading the bootstrap key or entering `ssh_exec`, it
   awaits `probe(host, port)`. A `reachable` verdict proceeds unchanged; an unreachable verdict
   raises at once. The probe already retries the ~46 ms sshd-bind race (ADR-0289) internally for up
   to its ~15 s deadline, so a still-unreachable verdict is authoritative — it *is* the race-tolerance
   budget, replacing the old 90 s × `max_attempts` window for the unreachable case.

2. **Name the reason from the shared vocabulary.** The probe's fixed `detail`
   (`unreachable` | `no SSH banner`) maps onto the #1008 `SshFailureReason` closed vocabulary
   (`unreachable` | `banner_timeout`). The raised `CategorizedError` carries
   `details={"reason": …, "detail": …}` — both leak-safe scalars that survive the worker's
   `_failure_context` → `Redactor` path into the persisted job record, so `jobs.get`/`jobs.wait`
   name *why* in seconds.

3. **Terminal fast-fail.** The pre-flight error is `terminal=True`, so the worker dead-letters it
   instead of requeuing. A guest the probe already found unreachable will not become reachable on a
   re-run of the same probe, so requeuing only re-burns wall-clock. This is what bounds *total*
   wall-clock: one ~15 s probe instead of three ~90 s windows.

4. **Document the single-window bound.** `SshRetryPolicy`'s docstring now states that `deadline_s`
   bounds attempt *start*, not in-flight completion, and does not bound cross-attempt wall-clock —
   the pre-flight is what keeps the total bounded. No change to the loop logic: it was already
   correctly bounded to `deadline_s + one final run_once`; the audit confirmed the overrun was the
   requeue multiplication, not a runaway loop.

No migration, schema, RBAC, or config change. The append path (a reachable guest whose first append
SSH races the bind window) keeps its existing retry semantics unchanged — it is only ever reached
now after the probe has already seen a banner.

## Consequences

- `authorize_ssh_key` against an unreachable guest fails in ~15 s (the probe deadline) with a named
  `reason`, terminally, instead of a multi-minute `running` then an opaque `255`. The agent can
  self-correct at once.
- Total wall-clock for the doomed case drops from ~230 s (three requeued ~90 s windows) to one
  ~15 s probe. Asserted two ways: a handler test pins `terminal is True` + the named reason + that
  `ssh_exec` is never called, and a `run_ssh_with_retry` test injects a clock and a
  time-consuming, always-retryable attempt to prove the single-window ceiling is `deadline_s` plus
  one final attempt (no runaway accumulation).
- The pre-flight trades the old 90 s × 3 tolerance for the probe's ~15 s tolerance of a slow-binding
  sshd. That is ~300× the measured ~46 ms bind race (ADR-0289) and matches the tolerance
  `check_ssh_reachable` itself uses, so a guest that legitimately needs longer than ~15 s to bind
  sshd is a separate, visible signal (the same probe reports it) rather than a silent doomed retry.
- The append path's retry window is now backstopped by the pre-flight and is reached only on a guest
  that already presented a banner, so its residual `deadline_s + per-attempt` ceiling applies only
  to a genuinely flaky post-banner append, not to the doomed-guest class this ADR targets.
