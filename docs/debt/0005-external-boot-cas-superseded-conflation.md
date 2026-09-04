# 0005 — `CasStatus.SUPERSEDED` conflates three distinct external-boot failures

## Status

Resolved (2026-09-04)

## Concern

`ExternalBootActivationRepository` reports a failed authority-fenced mutation as
`CasStatus.SUPERSEDED` without distinguishing why the predicate did not match. Its `_miss`
helper (`src/kdive/db/external_boot_activations.py`) returns `NOT_FOUND` when the row is
absent and `SUPERSEDED` otherwise, and `_authorized_row` folds the activation id, System id,
operation owner, authority generation, expected state, and `NOT cleanup_complete` into one
predicate. A caller that receives `SUPERSEDED` from `begin_recovery_attempt` cannot tell
whether the activation moved to another state, another actor's authority superseded its
own, or — separately — whether the reservation the attempt needs is not yet `ready`.

That opacity is deliberate at the repository boundary: the class docstring says it exists to
avoid disclosing which authority predicate mismatched, which is the right default for a
fencing protocol. The debt is at the **caller** boundary, where the three cases need
different agent-facing responses. A stale conflict identity should tell an administrator to
re-read `systems.get` and retry with a fresh digest; an unready reservation should tell them
to wait; a superseded authority generation should tell them another operation took over. One
`conflict` envelope for all three sends the wrong recovery action for two of them.

## Why deferred

The concern has no subject on this branch. Issue #2117 delivers the external-boot admission
matrix and three admission-and-authorization contracts that commit no activation transition;
`begin_recovery_attempt` has no caller in `src/`, and
`src/kdive/mcp/tools/external_boot/recovery_requests.py` is gated by a test asserting its
import closure reaches no activation-writing name.

The proposed remedy — read the reservation under the same System lock and refuse an unready
one with its own `reservation_not_ready` reason before calling `begin_recovery_attempt` — is
conditioned on making that call. Implementing it in #2117 would mean writing the transition
the operator's 2026-09-02 scope amendment explicitly excludes, and would reopen the one-way
door into `recovering` that the amendment closed.

## Non-regression boundary

- `src/kdive/mcp/tools/external_boot/recovery_requests.py` must not gain a
  `begin_recovery_attempt` call while this record is open and the executor is absent; the
  import-closure gate in `tests/services/external_boot/test_recovery_requests.py` holds it.
- `_miss` and `_authorized_row` must keep returning an opaque status **at the repository
  boundary**. Resolving this record means adding discrimination in the calling service, not
  widening what the repository discloses about which predicate failed.

## What would resolve it

When #2118 lands the recovery executor and the transition, have the calling service read the
activation and its reservation under the System lock it already holds and map the three
cases to distinct agent-facing reasons before invoking the CAS — at minimum
`observed_identity_stale`, `reservation_not_ready`, and `authority_superseded` — so each
carries its own recovery action.

Done when a conflict-resolution call against a stale digest, one against an unready
reservation, and one against a superseded authority generation return three different
`reason` values with three different next actions, each covered by a test.

## Resolution

Migration 0128 classifies losing commits while the relevant rows remain locked. It returns
`observed_identity_stale` for a missing or mismatched activation/System/Run identity,
`reservation_not_ready` when the required reservation is not `ready`, and
`authority_superseded` when the operation owner or authority generation no longer matches.
It also validates the exact reason, next-action, category, and terminal tuple before accepting a
failure result.

The worker consumes those database outcomes in `_classified_external_boot_failure` and emits the
closed mappings `systems.get` / `stale_handle` / terminal, `jobs.wait` /
`infrastructure_failure` / non-terminal, and `jobs.get` / `stale_handle` / terminal. Repository
statuses remain opaque; the discrimination is confined to the calling transaction and worker
boundary as this record required.

Executable evidence:

- `tests/db/test_migration_0128_external_boot_reentry_failures.py` pins migration installation,
  locked-row classification, and the three exact failure shapes.
- `tests/jobs/test_external_boot_authority_models.py` rejects every mismatched reason/action,
  category, or terminal combination.
- `tests/jobs/test_worker.py` proves the worker consumes each classified losing commit with its
  distinct lifecycle outcome.
- `tests/jobs/handlers/external_boot/test_lifecycle.py` proves authority supersession is reported
  distinctly without converting the activation result into success.
- `tests/jobs/handlers/external_boot/test_runner.py` enumerates every provider failure category,
  proves every mapped result is accepted by the database commit, pins the committable tuple, and
  proves serialized failures omit injected raw text and provider identifiers.

## Provenance

target: src/kdive/db/external_boot_activations.py
target: src/kdive/mcp/tools/external_boot/recovery_requests.py
Found by the `$gauntlet` design pass on the #2117 branch on 2026-09-02 (finding 3 of 12),
dispositioned `blocked` there, then re-dispositioned `rejected-with-evidence` for this branch
under the operator's scope amendment. The `$oathbind` scope audit the same day accepted that
disposition and noted the finding had no owning record; this is that record.
tracker: #2118
