# ADR 0581: Diagnose hosted provision readiness at persisted boundaries

## Status

Accepted

## Context

Hosted Ubuntu 26.04 ppc64le TCG runs leave a System in `provisioning` for the full state deadline.
PR #2057 exposed the state timeline and worker journal. PR #2060 attributed the stall to a
`state-fenced` provision lane, but current source routes `JobKind.PROVISION` to `default`; only
restore, reprovision, and snapshot are state-fenced. The post-#2060 journal contains a running fixed
worker and no claim or provider evidence, so it cannot distinguish an unclaimed default-lane row
from a claimed handler/provider stall. Increasing the deadline would hide the same ambiguity.

## Decision

The hosted proof records the queue and execution boundaries before changing timing:

1. A bounded, read-only queue snapshot reports a provisioning System's provision job id, persisted
   lane, job state, attempt, worker id, enqueue time, claim time, and lease expiry before cleanup.
2. A fixed worker logs its accepted lanes at startup and each successful claim with the job's
   persisted lane and database-derived queue delay.
3. Local-libvirt logs entry into each synchronous provision stage without logging paths, profile
   data, domain XML, guest output, or credentials.
4. The first hosted run selects the source correction from the first boundary lacking its expected
   successor. The final hosted run must reach `ready` and pass the named SSH proof.
5. The 600-second state deadline stays unchanged unless the boundary evidence measures ongoing
   hosted work at that deadline. Any later change must cite that hosted measurement; the separate
   post-ready SSH budget is not provision-timing evidence.

The diagnostics remain after the fix so every future red hosted proof identifies its own boundary.

## Consequences

A successful and a failed hosted run both expose persisted lane and claim timing. A queued row and
a running row no longer look alike. A claimed provider stall names the last entered stage. The
workflow gains one bounded local database read, and normal workers gain a few INFO lines per
provision; no MCP contract or database schema changes. Diagnostics stay observational and cannot
turn a red proof green or a green proof red.

## Considered & rejected

- **Accept PR #2060's state-fenced premise and widen the worker lanes again.** verified: at commit
  `66541e9d7a59922b9fb180a40284bc3370f68a04`,
  `dispatch_lane_for_kind(JobKind.PROVISION)` returns `default` because `PROVISION` is absent from
  `STATE_FENCED_JOB_KINDS`; the premise does not describe the persisted row.
- **Increase the provision state deadline from the SSH budget.** verified: hosted run 32968397867
  spent 600 seconds in `provisioning`, while the 900-second SSH budget starts only after `ready`;
  the latter measures a different phase and cannot justify the former.
- **Capture only the worker journal.** verified: hosted runs 32604993978 and 32968397867 show only
  worker process startup while the System remains `provisioning`; without the row's state and claim
  columns, both an idle worker and a busy silent handler fit the evidence.
- **Expose queue internals through the public MCP job envelope.** judgment: that widens a public
  contract and unrelated consumers when an issue-local, read-only hosted diagnostic settles the
  question with less surface.
- **Remove diagnostics after the source fix.** judgment: a future regression would recreate the
  evidence gap and require another hosted instrumentation cycle; bounded metadata-only evidence is
  cheaper to retain.
