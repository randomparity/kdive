# Spec — Synchronous-tool transport bound (#452)

- **Issue:** [#452](https://github.com/randomparity/kdive/issues/452) (work item C of epic #449)
- **ADR:** [`0126`](../adr/0126-synchronous-tool-transport-bound.md)
- **Design:** [`mcp-onboarding-error-ergonomics.md`](../design/mcp-onboarding-error-ergonomics.md) §"Work item C"
- **Date:** 2026-06-16

## Problem

A `systems.provision` call returned a raw "The socket connection was closed unexpectedly" — a
transport-level drop, not an envelope — and a retry succeeded. `systems.provision` enqueues a job
and returns fast (`src/kdive/mcp/tools/lifecycle/systems/registrar.py:102-130`), so the drop is
most consistent with a **synchronous blocking call** in the admission request path stalling the
asyncio event loop long enough for the client to time out. FastMCP runs over streamable HTTP
(ADR-0010) with no per-tool request timeout (`src/kdive/__main__.py:333`), so a stall surfaces as
a dropped socket rather than a typed error.

### The blocking call (spike result)

`systems.provision` → `SystemProvisionHandlers.provision_system` →
`SystemAdmission.create_for_allocation` (`src/kdive/services/systems/admission.py:191-242`). Every
DB access on that path already runs through `psycopg`'s **async** connection. The one synchronous
seam is the provider `rootfs_validator` callable
(`RootfsValidator = Callable[[RootfsSource], None]`), invoked synchronously inside the admission
path via `validate_rootfs_for_provider` (`src/kdive/services/systems/validation.py:36-46`), called
from `_new_system_allowed` (`admission.py:505-508`). For the `local-libvirt` provider that callable
is `provisioning.validate_rootfs_ref` (`composition.py:123`), which **materializes the rootfs base**
(`_materialize_rootfs_base` → `materialize_rootfs_base`,
`local_libvirt/lifecycle/provisioning.py:213-224`) — synchronous disk/qcow2 I/O that blocks the
event loop. A slow or large rootfs materialization is the head-of-line block. (The `fault-inject`
and `remote-libvirt` validators are `lambda _rootfs: None` and do not block, but the seam is the
generic risk.)

The blocking call sits in the **pre-mutation segment**: `_new_system_allowed` runs the quota check
and rootfs validation *before* the first `INSERT`/`enqueue` (`_insert_system_and_activate`,
`_enqueue_provision_job`). This is exactly the segment ADR-0126 bounds.

## Decision (per ADR-0126)

Two changes, both confined to the admission/validation seam:

1. **Offload the blocking validator** so one request cannot stall the event loop. The synchronous
   `rootfs_validator(rootfs)` call is moved to `asyncio.to_thread`, so the slow materialization runs
   on a worker thread and the event loop stays free for concurrent requests.

2. **Bound the pre-mutation segment** of `create_for_allocation` with an execution-time timeout
   (`asyncio.timeout`) that returns a `transport_failure` envelope (`ErrorCategory.TRANSPORT_FAILURE`,
   carrying ADR-0123's `detail`) instead of letting the socket drop. The timeout covers **only** the
   pre-mutation segment (profile validation, lock acquisition, quota + rootfs check). Once the first
   mutation is reached the deadline is **disabled** (`timeout.reschedule(None)`) and the request runs
   to its own completion and returns its real envelope.

### Where the boundary-disable fires (call-stack threading)

`create_for_allocation` owns the `asyncio.timeout(...)` context. The first mutation does not happen
at the top level — it sits one or two frames down, and the boundary **differs per branch**:

- new-System provision: `_insert_provisioning_system` runs `_new_system_allowed` (pre-mutation),
  then `_insert_system_and_activate` (first mutation);
- existing PROVISIONING System: `_enqueue_provision_job` is the first (and only) mutation, with **no**
  preceding slow work;
- existing DEFINED / terminal: returns a failure with **no** mutation at all.

So the disable signal must reach the precise first-mutation site. The timeout handle
(`asyncio.Timeout`, the object returned by `async with asyncio.timeout(...) as t`) is threaded as an
explicit parameter into `_provision_create_response` / `_define_create_response` and their callees;
each calls `timeout.reschedule(None)` on the line **immediately before** its first state-changing
DB call (`_insert_system_and_activate`, the `SYSTEMS.update_state` in `_admit_defined`, and
`_enqueue_provision_job`). A no-mutation branch never disables the deadline — correct, because it
also never mutates, so a timeout there is safe. Threading one parameter (rather than a module global
or a contextvar) keeps the boundary **visible at every mutation site** and makes a future refactor
that adds a mutation without disabling the deadline a reviewable, test-covered (acceptance #4) defect.

### Why the segment boundary (not the whole body)

Python cannot kill a running thread: an `asyncio.timeout` firing over a `to_thread` future cancels
the *awaiting coroutine* but the OS thread completes the materialization anyway. If the timeout
fired *after* a mutation began, the mutation would land while the caller is told `transport_failure`
— and because `transport_failure` is `retryable=True` (`responses.py:44`) the caller would
auto-retry and risk a double-provision. Bounding only the pre-mutation segment removes that hazard:
no mutation has happened when the timeout can fire.

The mutation segment stays **unbounded**, which is safe because it is DB-only and sub-second
(libvirt provisioning is worker-owned, enqueued not executed in the request path). A stall *within*
the mutation segment is not converted to an envelope; this is the bounded residual ADR-0126 accepts.

### Why the retry is safe

A `transport_failure` retry for an allocation whose provision already landed takes the
allocation-locked existing-System path: `_locked_allocation_system` →
`_find_system_for_allocation` resolves the existing System under the `ALLOCATION` advisory lock, and
`_provision_create_response` returns the existing job (re-enqueue is deduped on the
`{allocation_id}:provision` key, `admission.py:399-414`). So the retryable classification does not
create duplicate Systems/allocations — **this dedup already exists**; this slice adds no new
idempotency mechanism.

### Cancellation safety of the bounded segment

When the timeout fires inside `_locked_allocation_system`'s `async with conn.transaction()`, the
`CancelledError` propagates out of the `async with`, the transaction context manager **rolls back**,
and the `PROJECT`/`ALLOCATION` advisory locks auto-release at transaction end
(`db/locks.py:71-98`). No partial write survives. The connection returns to the pool cleanly. The
`to_thread` worker still finishes the materialization (the orphaned-thread residual ADR-0126 and the
#453 reachability probe both document and accept), but it touches only provider-local rootfs storage
and holds no DB row.

### The threshold

The bound is a single configurable seconds value set **above** the legitimate worst-case
pre-mutation latency. The pre-mutation work is: one probe `SELECT` (allocation), the lock waits, two
small `SELECT`s (quota count, existing-System lookup), and the offloaded rootfs materialization.
Materializing a qcow2 base is the dominant term; the default is **30 s**, generous for a local copy
while still converting a genuinely wedged materialization into a typed retryable error rather than a
silent socket drop. The value is read from config (`KDIVE_PROVISION_PREMUTATION_TIMEOUT_S`,
defaulting to `30.0`) so an operator with an atypically slow rootfs store can raise it without a code
change; the production default is the shipped value.

## Files

| Change | File |
|---|---|
| `validate_rootfs_for_provider` → async; `await asyncio.to_thread(rootfs_validator, rootfs)` | `src/kdive/services/systems/validation.py` |
| Await the now-async rootfs validation in `_new_system_allowed`; wrap the pre-mutation segment of `create_for_allocation` in `asyncio.timeout`; `reschedule(None)` at the mutation boundary; map `TimeoutError` → `AdmissionFailure(TRANSPORT_FAILURE, detail=...)` | `src/kdive/services/systems/admission.py` |
| `KDIVE_PROVISION_PREMUTATION_TIMEOUT_S` config setting (default `30.0`) | `src/kdive/config/core_settings.py` |
| Unit tests (bound fires → `transport_failure` envelope; offload → concurrent request not stalled; retry-after-timeout dedups; mutation-segment stall stays unbounded) | `tests/services/systems/test_admission_transport_bound.py` (new), `tests/mcp/lifecycle/test_systems_tools.py` |

No DB migration. The `mcp/` surface is untouched — the envelope is produced through the existing
`AdmissionFailure` → `ToolResponse.failure(detail=...)` projection (`provision.py:43-56`,
`responses.py:166-183`), so this slice stays disjoint from concurrent #451 (registrar
input-binding) except for not editing the registrar at all.

> **Scope note vs. the dispatch task framing.** The issue/orchestration brief mentions "wrap
> synchronous tool bodies with an execution-time bound" and "offload the blocking call(s) in the
> provision request path". The spike shows the only synchronous blocking call on the provision
> request path is the rootfs validator inside admission, and ADR-0126 scopes the bound to the
> **pre-mutation segment of `systems.provision`**, not a surface-wide tool-body wrapper. A generic
> middleware that wraps *every* tool body would (a) be unable to find the per-tool mutation boundary
> and so would either bound nothing safely or risk the double-provision hazard above, and (b)
> contend with concurrent #451's registrar edits. The bound therefore lives in the admission service
> at the one boundary the ADR identifies. No other tool has been shown to run a synchronous blocking
> call on the request path; adding a speculative surface-wide wrapper is out of scope (and against
> the "no speculative features" rule).

## Acceptance (each gets a test)

1. **Bound fires → `transport_failure` envelope, not a drop.** With an injected `rootfs_validator`
   that blocks past the bound and the timeout set very low, `create_for_allocation` (mode
   `provision`) returns an `AdmissionFailure` with `category=TRANSPORT_FAILURE` and a non-`None`
   `detail`; projected through `_admission_response` it is a `ToolResponse` with
   `error_category="transport_failure"`, `retryable=True`, and a `detail` string. No exception
   escapes (no socket drop), and **no System / job is written** (the bound fired pre-mutation).

2. **Offload → a slow validator does not stall a concurrent request.** With an injected
   `rootfs_validator` that sleeps (a `threading.Event`-gated block, the slow boundary double), a
   concurrent independent coroutine awaited on the same event loop completes promptly while the
   provision is still blocked in the validator — asserting the validator runs off the loop. (Bound
   set high so the offload, not the timeout, is what's exercised.)

3. **Retry after a transport bound dedups.** The existing
   `tests/mcp/lifecycle/test_systems_tools.py::test_provision_retry_is_idempotent` already pins this:
   a second `provision_system` for the same allocation (the retry the client makes after a
   `transport_failure`) returns the **same** job id and `system_id`, with one System row and one
   `granted->active` audit. This slice does **not** add a duplicate test; it confirms the existing
   coverage still holds after the timeout/offload changes (ADR-0126's "retry is deduped by the
   allocation lock — confirm, don't rebuild"). If that test regresses under the refactor it is a
   real defect, not a missing test.

4. **Mutation-segment stall stays unbounded.** A double that blocks *after* the mutation boundary
   (i.e. the deadline has been `reschedule(None)`-disabled) does **not** raise `TimeoutError` even
   with the bound set below its delay — the request runs to completion and returns its real envelope.
   (Pins the `reschedule(None)` boundary so a future refactor that drops it is caught.)

## Test strategy

Mock the **boundary** — the synchronous `rootfs_validator` callable — not the admission logic.
Acceptance 1/2/4 inject a slow/blocking validator (a `threading.Event` the test controls, so the
block is deterministic, not wall-clock-flaky); acceptance 3 is the pre-existing
`test_provision_retry_is_idempotent`. The timeout threshold is injected/overridden per test (a very low bound for "fires", a
high bound for "offload") so no test waits real seconds. The integration tests run against
`migrated_url` with `asyncio.run`, matching the existing `tests/mcp/lifecycle/test_systems_tools.py`
shape. Honor existing gating: all new tests run in the default suite; no live suite is un-gated.
