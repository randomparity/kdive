# Plan — Synchronous-tool transport bound (#452)

- **Spec:** [`docs/specs/2026-06-16-synchronous-tool-transport-bound.md`](../../specs/2026-06-16-synchronous-tool-transport-bound.md)
- **ADR:** [`0126`](../../adr/0126-synchronous-tool-transport-bound.md)
- **Branch:** `feat/transport-failure-envelope-452`
- **No DB migration.**

TDD throughout: write the failing test, confirm it fails for the right reason, write the minimal
implementation, refactor green. Each step ends with `just lint && just type && just test`.

The blocking call confirmed by the spike: `systems.provision` → `create_for_allocation` →
`_new_system_allowed` → `validate_rootfs_for_provider` → the synchronous `rootfs_validator`
callable (for `local-libvirt`, a qcow2 materialize). It sits in the pre-mutation segment.

## Step 1 — Offload the synchronous rootfs validator (foundation)

**Test first** (`tests/services/systems/test_admission_transport_bound.py`, new). Deterministic
ordering protocol (no `sleep`-based races):
- Inject a `rootfs_validator` that, when called, **sets** a `validator_entered` `threading.Event`
  and then **blocks** on a `release` `threading.Event` the test owns (never set until the assertion
  below has run).
- Start `provision_system` as a task. `await` (with a small `asyncio.wait_for` ceiling) a
  **separate** trivial coroutine that flips a `concurrent_ran` flag and returns. With the offload in
  place, the blocking validator runs on a worker thread, the loop stays free, and the concurrent
  coroutine completes → `concurrent_ran is True`. **Without** the offload the validator runs inline
  on the loop, the concurrent coroutine is starved, and `asyncio.wait_for` raises `TimeoutError` →
  the test fails for the right reason. Use `validator_entered.wait()` first so the assertion only
  runs once the provision is genuinely inside the validator.
- Then `release.set()` and `await` the provision task; assert it finished and the validator recorded
  its call (so the test cannot pass vacuously). This is **acceptance #2**.

**Implement** (`src/kdive/services/systems/validation.py`):
- Make `validate_rootfs_for_provider` **async**; replace `rootfs_validator(rootfs)` with
  `await asyncio.to_thread(rootfs_validator, rootfs)`. Keep the `None`/`_UploadRootfs` early returns
  synchronous (no thread for a no-op). `import asyncio`.

**Implement** (`src/kdive/services/systems/admission.py`):
- `_new_system_allowed` is already `async`; change its `validate_rootfs_for_provider(...)` call to
  `await validate_rootfs_for_provider(...)`. Same for the one call in `_provision_defined_response`
  (`validate_rootfs_for_provider` at `admission.py:479`), which is also already inside an `async`
  function — keep `provision_defined` correct even though the spike targets `provision`.

**Verify:** new offload test passes; existing admission/systems tests stay green (validator seam
unchanged, only its invocation is now awaited-in-thread).

## Step 2 — The pre-mutation timeout bound + `transport_failure` mapping

**Test first** (`tests/services/systems/test_admission_transport_bound.py`):
- **Acceptance #1 (bound fires → envelope, not drop):** inject a `rootfs_validator` that blocks
  past the bound; override the bound to a very low value (inject via a `premutation_timeout_s`
  constructor/parameter seam — see Implement). `create_for_allocation(mode="provision")` returns an
  `AdmissionFailure` with `category is ErrorCategory.TRANSPORT_FAILURE` and `detail is not None`. No
  exception escapes. Assert **no System row and no job row** were written (query the DB) — the bound
  fired pre-mutation. Project it through `_admission_response` and assert the `ToolResponse` has
  `error_category == "transport_failure"`, `retryable is True`, and a `detail` string.
- **Acceptance #4 (mutation-segment stall stays unbounded — `reschedule(None)` fires):** assert the
  boundary directly with a **spy timeout handle**, the deterministic primary (not a sleep-based slow
  mutation, which has no clean injection point). Inject a fake `timeout` factory into
  `create_for_allocation` (a `timeout_factory: Callable[[float], asyncio.Timeout] | None = None`
  field on `SystemAdmission`, defaulting to `asyncio.timeout`) whose returned handle records every
  `reschedule(...)` call and its argument. Run a normal fast `provision_system` (new System, fast
  validator) and assert: (a) the recorded handle saw exactly one `reschedule(None)` call, and (b) it
  was recorded **before** the System row exists — checked by having the spy capture, on
  `reschedule(None)`, a `SELECT count(*) FROM systems` against the same pool (0 rows at disable time)
  via a callback the test wires, OR more simply assert ordering by recording a monotonic step counter
  the insert also bumps. Pinning the call proves a future refactor that adds a mutation without
  disabling the deadline is caught. The same spy run, repeated for the existing-PROVISIONING
  re-enqueue branch and the DEFINED→provisioning admit branch, asserts each mutation site disables
  the deadline exactly once and the no-mutation failure branch disables it **zero** times.

**Implement** (`src/kdive/config/core_settings.py`):
- Add `_positive_float(raw)` parser (`float(raw)`; raise `ValueError` if `<= 0`).
- Add `PROVISION_PREMUTATION_TIMEOUT_S = Setting(name="KDIVE_PROVISION_PREMUTATION_TIMEOUT_S",
  parse=_positive_float, default="30.0", group="lifecycle", processes=_SERVER, help="...",
  suggest="a positive number of seconds, e.g. 30")`. Append to the `SETTINGS` list.

**Implement** (`src/kdive/services/systems/admission.py`):
- `SystemAdmission` gains an optional `premutation_timeout_s: float | None = None` field (frozen
  dataclass). `create_for_allocation` resolves the effective bound:
  `self.premutation_timeout_s if not None else config.get(PROVISION_PREMUTATION_TIMEOUT_S)` (the
  setting has a default so `config.get` never returns `None`; assert/`or 30.0` as a belt-and-braces
  fallback). The injectable field is the test seam (no env mutation in tests).
- Wrap the pre-mutation body of `create_for_allocation` in `async with asyncio.timeout(bound) as t:`.
  The pre-lock `validate_profile_for_provider` and the `_locked_allocation_system` acquisition +
  `_new_system_allowed` run **inside** the timeout. Thread the handle `t` into
  `_provision_create_response` / `_define_create_response`.
- In `_insert_provisioning_system` / `_insert_defined_system` / `_admit_defined` /
  `_enqueue_provision_job` (the existing-PROVISIONING fast path) and the `existing` re-enqueue
  branch, call `timeout.reschedule(None)` on the line **immediately before** the first
  state-changing DB call (`_insert_system_and_activate`, `SYSTEMS.update_state`, `queue.enqueue`).
  Pass the handle down as an explicit parameter. A no-mutation failure branch (DEFINED/terminal)
  never disables — correct.
- Catch `TimeoutError` around the whole bounded block (sibling to the existing
  `except IllegalTransition`): return `_failure(request.allocation_id,
  ErrorCategory.TRANSPORT_FAILURE, detail="provisioning admission exceeded the {bound}s bound; "
  "retry", suggested_next_actions=("systems.provision",))`. `TRANSPORT_FAILURE` is not a suppressed
  category, so the detail surfaces.

**Verify:** acceptance #1 and #4 pass; the existing
`test_provision_retry_is_idempotent` / `test_provision_mints_system_active_allocation_and_job` /
quota / non-granted tests stay green (the bound is generous; the boundary-disable is on the mutation
path they already exercise). This green run is **acceptance #3** (retry dedup confirmed unbroken).

## Step 3 — Regenerate the config reference + full guardrails

- `just config-docs` to regenerate `docs/guide/reference/config.md` for the new setting (CI runs
  `config-docs-check`); the read is through `config.get` inside `kdive.config`-consuming code so
  `config-guard` and `env-docs-check` stay green.
- Run the full superset `just ci`. (`check-mermaid` may fail locally on a missing `jsdom`; if that is
  the **only** local failure, note it in the PR body and proceed — CI provisions it.)

## Risk / edge notes

- **Cancellation safety:** a `TimeoutError` inside `_locked_allocation_system`'s
  `async with conn.transaction()` rolls the transaction back and auto-releases the advisory locks
  (`db/locks.py`); no partial write survives. Verified by acceptance #1 asserting zero System/job
  rows after the bound fires.
- **Orphaned validator thread:** the `to_thread` worker finishes the materialization after the
  timeout (Python cannot kill it); bounded and provider-local, accepted by ADR-0126.
- **`provision_defined` path:** also awaits the now-async `validate_rootfs_for_provider`; it is not
  bounded by this slice's timeout (the spike scope is `provision`), but the offload keeps its
  validator off the loop too — a strict improvement, no regression.
- **No new tool / no `mcp/` edit / no registrar edit:** keeps the diff disjoint from concurrent #451.
