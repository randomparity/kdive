# Engine-owned debug lifecycle

## Problem

The resolver interprets four engine-private fields to enumerate sessions, decide cleanup and
stop/drain operations. The engine owns those resources but lacks the corresponding lifecycle
boundary. Current runtime source is unchanged from the campaign base; prior staging/halt fixes
protect different contracts and remain intact.

## Scope

Move existing policies into DebugEngineRuntime (C1): owned_session_ids snapshot, async sweep_once,
begin_close and async drain. Resolver owned/sweep delegate; it retains guarded provider enumeration,
cache, scheduling and lifespan. No new module/protocol/framework or compatibility path.

Preserve per-session locking, five-second DB timeout/query, live exemption, pending-cleanup bypass,
off-thread reap, retry logs and root/attachment retention (C2). Close every engine under the current
resolver guard before awaiting periodic stop; drain all admitted work before the final sweep.
Preserve _finish_task, unpublished first attach, cancellation/error retrieval and reap/lock_for
callers. Assembly and session lifecycle contracts do not change. ADR0862 records the owner split.

### Failure model

Retain existing process-scoped event-loop/request-thread ownership and registry locks. Snapshots
have no ordered-session contract. Existing cleanup faults retain owners for retry; neither final
sweep nor close guarantees native release. No new arbitrary-thread API or cancellation policy.
DB outages and live rows keep current eligibility rules; pending cleanup retains its bypass.
No public/schema/auth, concurrency-policy, dependency, deployment or guest-memory changes.
Constructor I/O, session, console and final campaign gates retain their independent owners.

## Success

Resolver orchestration works through engine lifecycle operations without reading engine-private
bookkeeping (C1). All runtimes reject admission before any drain; admitted work finishes before
final cleanup, including unpublished first attach. Existing failure/retry behaviors remain (C2).
Commit locally and attribute the exact finding; originating debug outcome/assessment stays pending
until its actual dependency/lifetime proof and changed-source assessment exist (C4).

## Validation

focused-test: extend tests/mcp/debug/test_runtime_lifetime.py with a public-only engine boundary
using real engine logic and SDK/DB fakes, registered/pending ownership, close admission and a
controlled two-engine shutdown. Require ownership-seam RED and discriminating dependency/drain
faults with exact restoration. Retain live/outage/retry/cancellation/first-attach proofs (C3).
Run Docker-required just test-verbose for that file and tests/mcp/debug/test_debug_ops.py plus
relevant unchanged session callers; lint, whole-tree types/hooks and unrelated AST/198SQL checks.

task-test-not-applicable: no private-name/prose snapshots or new live-VM claim. Existing caller
proofs protect public behavior; final campaign CI/whole-branch reviews retain their owners.
Fresh scoped code/security review and exact committed-byte verification precede resolution (C4).
