# ADR-0306: attach guest console evidence to an SSH transport failure (#1009)

- Status: Accepted
- Date: 2026-07-02
- Builds on [ADR-0271](0271-system-direct-ssh-access.md) (`authorize_ssh_key`),
  [ADR-0298](0298-ssh-reachable-runtime-probe.md) (`check_ssh_reachable` banner probe and its
  `refs.result` verdict), [ADR-0235](0235-per-run-console-evidence.md) (the per-Run console
  read + `Redactor` path this reuses), [ADR-0302](0302-ssh-failure-reason-classification.md)
  (the host-side `stderr_tail`/`reason` this pairs with), and
  [ADR-0305](0305-authorize-ssh-reachability-preflight.md) (the fast-fail pre-flight whose
  `TRANSPORT_FAILURE` this now enriches). Sibling of the failure-diagnosability work #1008/#1011/#1012.

## Context

When `systems.authorize_ssh_key` fails with a `TRANSPORT_FAILURE`, the failure record is host-side
only. #1008 classifies *why ssh's client failed* (`reason`, `stderr_tail`); #1012 fast-fails an
unreachable guest with a named reason. But none of it correlates the failure with what the **guest**
was doing at that moment — did sshd start, did the NIC lease, did the loopback forward bind?

In the black-box review the reviewer had to reach for `control.diagnostic_sysrq` **in a separate
session** to observe sshd `Started` on the guest console — a manual side channel the failure itself
never pointed at. An agent hitting the failure live had no in-band way to answer "did the guest sshd
come up?".

`check_ssh_reachable` names the *lowest failing layer* (`tcp_connect`/`ssh_banner`, ADR-0303) but
still cannot say whether sshd ever started inside the guest.

## Decision

On a `TRANSPORT_FAILURE` from `authorize_ssh_key`, and on an **unreachable verdict** from
`check_ssh_reachable`, attach a **bounded, redacted tail of the System's console** so "did sshd
start?" is answerable from the failed job / verdict **alone**.

- **Reuse, don't fork.** A new lean `console_evidence` module holds the console read: `read_redacted_console`
  (moved verbatim from `boot_evidence`, which now re-imports it — single source of truth) plus a
  `redacted_console_tail(system_id, secret_registry, max_chars)` helper that slices the last
  `max_chars` characters of that already-redacted read. The redaction is the **same** `Redactor`
  path the boot console artifact uses (ADR-0235); no second redaction mechanism is introduced.
  The read is **best-effort**: any failure (empty log, non-root `PermissionError`, absent System)
  returns `None` and never masks the primary transport failure.

- **`authorize_ssh_key` — inline tail in `failure_context`.** The handler catches the
  `TRANSPORT_FAILURE` it is about to propagate (both the #1012 pre-flight fast-fail and the append
  failure), reads the console tail, and adds it to the `CategorizedError.details` as `console_tail`.
  The worker's existing `_failure_context` (untouched) already projects scalar details into
  `failure_detail_*`, re-redacting (idempotent) and capping each value — so the tail surfaces as
  `failure_detail_console_tail` on `jobs.get`/`jobs.wait`, exactly the envelope #1008's `reason`/
  `stderr_tail` already use. A **failed job has no `refs` field** (only `result_ref` on success and
  `failure_context` on failure), and the existing `console-<run_id>` artifact machinery is
  *Run*-scoped while these jobs are *System*-scoped with no `run_id` — so a linked artifact ref would
  require a **new** capture path, which the issue explicitly forbids. The inline tail is therefore
  the only surface that reuses existing machinery.

- **`check_ssh_reachable` — inline tail in the verdict.** An unreachable probe is a job *success*
  carrying a verdict in `refs.result`, not a failure, so there is no `failure_context` to enrich.
  The same helper's tail is added as an additive `console_tail` field on the verdict **only** when
  the guest is unreachable (a reachable guest needs no guest-side diagnostics), keeping the reachable
  verdict byte-for-byte back-compatible.

- **Bound.** `_CONSOLE_TAIL_MAX_CHARS = 800` characters. Larger than the host-side 512-char
  `stderr_tail` (a console line is noisier and the sshd-status signal may sit a few lines back), but
  kept **under** the worker's 1000-char `_CONTEXT_VALUE_MAX` so the *recent* tail survives — the
  worker head-slices `[:1000]`, so a tail longer than that cap would keep the *oldest*, wrong end.

## Consequences

- A failed `authorize_ssh_key` job's `jobs.get`/`jobs.wait` envelope carries
  `failure_detail_console_tail` — the guest's last ~800 redacted console characters — beside
  `failure_detail_reason`/`failure_detail_stderr_tail`. Host-side "why ssh failed" and guest-side
  "what the guest was doing" are now self-diagnosing from the failure alone.
- An unreachable `check_ssh_reachable` verdict carries the same `console_tail`; the reachable verdict
  is unchanged.
- The console tail is best-effort: an empty or worker-unreadable console simply omits the field.
- The `ssh_authorize` / `check_ssh_reachable` handlers and `systems.register_handlers` now take the
  worker `secret_registry` (already available at registration) to drive redaction.

## Rejected alternatives

- **A linked console-artifact `ref` in the failure.** Failed jobs carry no `refs`, and the reusable
  console-artifact path is Run-scoped (`console-<run_id>`); a System-scoped authorize/reachable
  failure has no Run, so this would mean building a new capture path — out of scope per #1009.
- **Editing the worker's `_failure_context` to special-case a console tail.** The scalar-detail
  projection already carries it; routing through `CategorizedError.details` keeps the worker generic.
- **A second, larger tail cap for the verdict path** (no worker head-slice applies there). Rejected
  for one shared, predictable bound across both surfaces.
- **Always emitting `console_tail` on the reachable verdict** (as `null`). Rejected to keep the
  reachable verdict byte-for-byte back-compatible; the field appears only where it has diagnostic value.
