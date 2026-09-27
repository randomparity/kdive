# Power provider I/O fence

## Problem

Issue #2831: ON, CYCLE, and RESET release their READY precheck lock before provider I/O, so a
force-crash job can commit CRASHING before an earlier power call reaches the guest. RESUME is a
separate state-changing power route and is explicitly included in the issue.

## Scope

The worker control handler owns the System fence; the provider port, MCP authorization, and
force-crash implementation stay intact. Reuse ADR-0685's session lock and cancellation rule for
each `PowerAction`; ADR-0687 amends its OFF-only clause. The local-libvirt clean-stop helper and
OFF wait remain.
No caller migration or obsolete path is needed: the handler remains the entry point. Work targets
Python 3.14 on x86_64 and ppc64le.

## Design

One fenced runner accepts an async operation, asserts an idle connection, temporarily enables
autocommit, holds the System session lock, then restores the prior mode on every exit. Its outer
task shield waits for the fenced operation to complete even after repeated cancellation. ON,
CYCLE, RESET, and OFF execute their existing READY precheck, controller resolution, and provider
call inside it; their existing post-call audit stays outside. RESUME executes its existing PAUSED
precheck, provider call, PAUSED-to-READY update, and audit inside it. A READY RESUME retry remains
an idempotent no-op. Provider failures propagate; cancellation propagates only after the provider
thread and lock cleanup finish. No additional timeout is imposed on non-OFF calls.
The provider-kind tag set during controller resolution is handed back from the shielded task to
the worker task on success, failure, and cancellation so existing telemetry retains its label.

## Failure model

- Actors and deployments: authenticated power jobs and force-crash jobs on local, remote, and
  fault-inject provider runtimes; x86_64 and ppc64le workers.
- Invariants and assets: CRASHING evidence must not be touched by a late power I/O call; the
  System lock must remain held through physical I/O; PAUSED-to-READY and audit semantics survive.
- Accepted failure classes: a blocking provider call can delay cancellation and force-crash; the
  provider supplies no bound for non-OFF calls. A provider failure leaves the current retry/error
  behavior and releases the fence after its thread exits.
- Covered elsewhere: OFF wait by #2801/ADR-0685; force-crash marker/NMI by control-plane owner;
  other lifecycle destroy sites by their own follow-ups.

## Success

For ON, CYCLE, and RESET, a crash marker attempt made while provider I/O is blocked waits until
that I/O finishes; a later power call sees CRASHING and does not start provider I/O. Repeated
cancellation keeps the fence through provider completion and restores connection mode. RESUME
holds the fence through its provider call and PAUSED-to-READY commit, retaining its idempotent
READY retry and audit behavior. OFF's existing tests remain green.
The worker still observes the provider-kind tag after success, failure, and cancellation.

## Validation

- focused-test: parameterized adversarial ON/CYCLE/RESET provider pause proves the crash lock
  cannot be taken before provider completion; red before the change, green with `uv run python
  -m pytest tests/adversarial/test_provider_state_races.py -q`.
- focused-test: repeat-cancel a blocked non-OFF provider call; prove lock held until thread exit,
  cancellation propagation, and autocommit restoration with the same command.
- focused-test: block RESUME provider I/O and probe its System lock, then verify PAUSED-to-READY
  state and audit with focused `tests/jobs/handlers/control/test_power_resume.py` cases.
- focused-test: existing OFF race and cancellation tests remain green with the adversarial command.
- focused-test: RESUME success and RESET provider failure/cancellation retain the worker-visible
  provider-kind tag in the direct handler and adversarial tests above.
- task-test-not-applicable: ADR/spec prose has no executable contract beyond the behavior above;
  use `just records` and doc-link guards for formatting/reference integrity.
- guardrails: `just lint`, `just type`, `just test-changed`, then coordinated pre-push `just ci`.
