# ADR 0483 — A non-retryable category dead-letters its job, and a denied guest-agent RPC is one

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** #1631
- **Extends:** [ADR-0118](0118-wait-on-resource-mechanisms.md), which made `retryable` a pure function
  of the failure category. That function was only ever applied to the response envelope; this ADR
  applies the same table at the second retry seam, the job queue.
- **Amends:** [ADR-0159](0159-guest-agent-deterministic-failure-classification.md), which classified guest-agent
  failures by libvirt error code. The code set stays exactly as it was; one message-shaped
  condition is added alongside it.

## Context

Proving #1610 against a Rocky 10 guest, every remote install failed. The guest image's
`/etc/sysconfig/qemu-guest-agent` shipped an RPC allowlist (`FILTER_RPC_ARGS --allow-rpcs=…`)
that omitted `guest-exec`, so the agent refused the call kdive's install path is built on. The
failure was permanent — no retry widens an allowlist — but the job made all three attempts, and
each one was reported as `transport_failure`, a category whose name says *the channel is flaky,
try again*. A five-second misconfiguration read as a slow, intermittent infrastructure problem.
The observation is recorded in
`deploy/ansible/roles/guest_base_image/tasks/build_one.yml`, which fixes the image.

Investigating it turned up **two independent defects stacked on each other**, and fixing either
one alone leaves the reported behaviour intact.

### Defect 1 — the denial is misclassified

The issue guessed that qemu-ga's refusal never reaches `classify_agent_libvirt_error` at all,
because qemu-ga answers a QMP command with a JSON `{"error": …}` payload inside an otherwise
successful libvirt call. **That guess is wrong, and it is worth stating so plainly because it is
the plausible-sounding explanation the next reader will also reach for.** libvirt's
`qemuAgentCheckError` inspects the reply, finds the `error` member, and raises. The denial *does*
arrive as a `libvirt.libvirtError` and *does* reach the classifier.

What it does not arrive with is a distinguishing error code. QEMU's `qmp_dispatch` refuses a
filtered command with `error_setg(…, "Command %s has been disabled%s%s")` — class
`GenericError` — and libvirt relays a `GenericError` as `VIR_ERR_INTERNAL_ERROR` (code 1), its
catch-all. `_DETERMINISTIC_CONFIG_CODES` holds six specific codes and, correctly, not that one.
So the denial fell through to the `TRANSPORT_FAILURE` default: retryable, and named after a
condition that was not occurring.

### Defect 2 — a non-retryable category did not actually stop the retries

This is the larger defect and the issue does not mention it. Correcting defect 1 in isolation
would have relabelled the failure and changed nothing about the three attempts.

`Worker._run_handler` decided whether to dead-letter with
`terminal = isinstance(exc, CategorizedError) and exc.terminal`. The **category was never
consulted.** `queue.fail` then dead-lettered only on that flag or on attempt exhaustion
(`DEFAULT_MAX_ATTEMPTS = 3`). A correctly-classified `CONFIGURATION_ERROR` burned all three
attempts exactly like an infrastructure blip.

`_RETRYABLE_BY_CATEGORY` — the ADR-0118 table that already answers this question, and which maps
`CONFIGURATION_ERROR` to `False` — lived in `kdive/mcp/responses.py` and drove only the
agent-visible `retryable` boolean. The envelope told the agent not to retry while the queue
retried anyway.

The safe behaviour was therefore opt-in, one `terminal=True` per raise site, and it was widely
missed. `jobs/handlers/image_build.py` documented dead-lettering on a `CONFIGURATION_ERROR` that
the code did not provide; `tests/jobs/test_worker.py` asserted the gap in words ("the terminal
flag, not the category, drives the immediate dead-letter") without anyone noticing it described
a bug. There are 13 `CONFIGURATION_ERROR` raise sites under `jobs/handlers/` alone, none of them
flagged.

## Decision

### 1. The category is the primary dead-letter signal; `terminal` becomes an escalation

`Worker._run_handler` now computes `terminal` from both inputs:

```
terminal = exc.terminal or not retryable_category(category)
```

A category the taxonomy calls non-retryable is permanent by construction, so re-dispatching it
can only reproduce the same failure more slowly, under a label that already told the caller not
to retry. `CategorizedError.terminal` keeps a real and distinct job: it **escalates** a failure
in a *retryable* category to an immediate dead-letter, for the case where this particular
failure already drove the target to a terminal state (a provision failure that left the System
`failed`). Every existing `terminal=True` site keeps its meaning; none is now redundant, because
each one sits on a retryable category.

Rejected: making this seam-local — having the guest-agent classifier set `terminal=True` on the
denial and leaving the queue as it was. It is a smaller and lower-risk change, and it fixes the
symptom in the issue. It was rejected because it fixes exactly one raise site out of the class,
leaves `image_build.py`'s docstring still describing behaviour that does not exist, and leaves
the next permanent failure to rediscover the same bug — the default stays unsafe and the correct
behaviour stays something each author must remember. This defect was found by accident while
proving an unrelated issue; that is not a detection strategy worth preserving.

### 2. One retryability table, in the domain layer

`RETRYABLE_BY_CATEGORY` and a `retryable_category()` accessor move from `kdive/mcp/responses.py`
to `kdive/domain/errors.py`, beside `ErrorCategory`. `responses.py` reads it from there; the
envelope's behaviour is unchanged.

The move is forced, not cosmetic: `kdive.jobs` must not import `kdive.mcp` (the dependency runs
the other way, and 14 modules under `mcp/` import `jobs/`). The alternative — a second copy of
the table in the queue plus a guard test pinning them together — would leave two answers to one
question, free to drift, with the tie-break living in a test. The queue's dead-letter decision
and the agent-visible `retryable` boolean are now the same fact read twice.

### 3. The denial is detected by message, and the fragility is the point

`classify_agent_libvirt_error` gains one message match:

```
_RPC_DISABLED_RE = re.compile(r"[Cc]ommand\s+(?P<rpc>[A-Za-z0-9_-]+)\s+has been disabled")
```

A match yields `CONFIGURATION_ERROR` naming the refused RPC and the file to edit —
*"qemu-guest-agent refused the 'guest-exec' RPC: the guest image's agent allowlist
(--allow-rpcs in /etc/sysconfig/qemu-ga) does not include it"* — plus a `denied_rpc` key in
`details` for downstream audit.

Rejected: adding `VIR_ERR_INTERNAL_ERROR` to `_DETERMINISTIC_CONFIG_CODES`. That code is
libvirt's catch-all and covers many genuinely transient conditions; under decision 1 every one of
them would become an immediate, unretried, permanent job failure. The blast radius is far larger
than the bug.

**Matching a QEMU string relayed through libvirt is fragile and this ADR does not pretend
otherwise.** The message is a QEMU implementation detail, wrapped by libvirt, with no stability
guarantee; the regex tolerates the variation QEMU already emits (leading capital, the optional
`: <reason>` suffix qemu-ga supplies, libvirt's own `internal error: unable to execute QEMU agent
command …` prefix) and nothing more. The mitigation is that the match is deliberately **one-way**:
it can only *upgrade* an otherwise-retryable error to `CONFIGURATION_ERROR`, never downgrade one.
If QEMU rewords the message the classification silently reverts to the pre-#1631 behaviour —
`transport_failure`, retried three times — which is slower and vaguer but never a transient fault
wrongly declared permanent. A drifted match costs the diagnosis, not correctness.

There is no non-fragile alternative available at this seam. libvirt does not preserve QEMU's QMP
error class, and probing the allowlist before each call would add a round-trip to every in-guest
command to detect a condition the guest image build already prevents.

## Consequences

**Every job kind's retry behaviour changes**, not just the remote-libvirt install path. A handler
failure in any of these categories now dead-letters on attempt 1 instead of attempt 3:
`configuration_error`, `missing_dependency`, `build_failure`, `install_failure`, `stale_handle`,
`lease_expired`, `not_implemented`, `not_found`, `symbol_not_found`, `conflict`,
`authorization_denied`, `quota_exceeded`, `allocation_denied`. The retryable half —
`infrastructure_failure`, `provisioning_failure`, `boot_timeout`, `readiness_failure`,
`transport_failure`, `transport_conflict`, `debug_attach_failure`, `control_failure`,
`capacity_exhausted`, `queue_timeout` — is untouched and still requeues.

Reviewed against the actual raise sites: these are host binaries that are not installed
(`missing_dependency`), artifact validation that rejected the payload (`build_failure`), an
in-guest install script that failed (`install_failure`), and admission denials
(`quota_exceeded` / `allocation_denied`). None becomes true because the same request ran again
seconds later. The requeue path has no backoff — it clears the lease and the job is immediately
re-claimable — so the retries it removes were never waiting for anything to change.

The cost is real and worth naming: a failure that *was* transient but got classified into a
non-retryable category now fails on the first attempt with no second chance. Before this change
a misclassification was papered over by two extra attempts. Miscategorisation is now visible
instead of absorbed, which is the correct trade but is a behaviour change for any handler
currently relying on that accidental cushion. `RETRYABLE_BY_CATEGORY` becomes the load-bearing
statement it always claimed to be, and its existing exhaustiveness and per-category pin tests in
`tests/mcp/core/test_responses.py` now gate two seams rather than one.

`tests/jobs/test_worker.py`'s attempt-exhaustion test moved from `BUILD_FAILURE` to
`INFRASTRUCTURE_FAILURE`: under this ADR a `build_failure` never reaches `max_attempts`, so the
old test could no longer exercise the path it was written for. Its neighbouring contract comment,
which asserted the pre-fix behaviour as if intended, is corrected.

No schema, no migration, no configuration setting, and no change to the MCP tool surface or to
any response envelope. `queue.fail` is unchanged — the decision is made by its caller.

Not addressed here: per-attempt retry backoff (the queue still re-dispatches immediately), and
whether `max_attempts` should be per-kind rather than a single `DEFAULT_MAX_ATTEMPTS = 3`.
Neither is needed to close #1631 and both would widen this change further.
