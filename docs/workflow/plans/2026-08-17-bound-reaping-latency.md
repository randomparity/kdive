# Implementation plan — bound reaping latency (#1980)

Goal: give the reconciler's two host-state reaping lanes a per-lane pass budget that is consulted
only between candidates, and give the remote-libvirt reaper opener a bounded TCP reachability gate,
so an unreachable provider host cannot hold a pooled connection idle-in-transaction for an untunable
~130 s — without ever ending a transaction while a provider call may still be mutating host state.

Architecture: two additive `KDIVE_*` settings; a new leaf module for the gate; a `budget` keyword on
each lane function and a monotonic deadline checked at the top of each candidate loop; one new
`ReconcileConfig` field threaded through `_REPAIR_CATALOG`.

Spec: `docs/workflow/specs/2026-08-17-bound-reaping-latency-design.md`.
Decision: `docs/adr/0565-bound-reconciler-provider-reaping-latency.md`.

## Global constraints

- Python 3.14, `uv`. Ruff line length **100**, lint set `E,F,I,UP,B,SIM`. `ty` strict, whole tree.
- `BASE_BRANCH` = `main`. Branch = `feat/bound-provider-reap-latency-1980`. Worktree =
  `/home/dave/src/kdive-worktrees/feat/bound-provider-reap-latency-1980`.
- Guardrails: `env -u FORCE_COLOR just ci` (full gate). Sub-recipes: `just lint`, `just type`,
  `just test`, `just config-docs` (regenerates `docs/guide/reference/config.md`).
  Run gates **bare** — no `| tail`, no `>/dev/null`, no `|| true`. This host is zsh, so
  `$PIPESTATUS` is a bash-ism; redirect to a file and read `$?`.
- A fresh worktree needs `npm ci --prefix .github/scripts/mermaid-check/` before `just ci`.
- Prose: **Milestone** not "Sprint"; avoid "critical", "robust", "comprehensive", "elegant" in
  comments, docstrings, and commit messages.
- Do not touch `.github/scripts/`, `.github/workflows/`, or `justfile` — concurrent siblings own
  them.
- Every limit surfaced to an operator or agent states all five parts: unit, reference clock, scope,
  consequence of violation, recovery action.
- Defaults: `KDIVE_RECONCILER_LANE_BUDGET_SECONDS` = `10`; `KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS`
  = `5`. libvirt TLS default port = `16514`.

## Task 1 — the reachability gate

**Creates** `src/kdive/providers/remote_libvirt/connection/reachability_gate.py`.
**Creates** `tests/providers/remote_libvirt/test_reachability_gate.py`.
**Modifies** `src/kdive/providers/remote_libvirt/settings.py`,
`src/kdive/providers/remote_libvirt/reaping/connections.py`.

**Interfaces produced** (later tasks and tests rely on these exact names):

```python
REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS: Setting          # settings.py
DEFAULT_LIBVIRT_TLS_PORT: int = 16514                    # reachability_gate.py
def remote_endpoint(uri: str) -> tuple[str, int]: ...
def require_reachable(
    uri: str,
    *,
    timeout: float,
    connect: Callable[[tuple[str, int], float], AbstractContextManager[object]] = ...,
) -> None: ...
def reaper_connect_timeout() -> float: ...
```

Steps, in order: write the failing endpoint-parser tests; run them and confirm they fail; implement
`remote_endpoint`; run and confirm green. Then the `require_reachable` tests (a real listening socket
on `127.0.0.1:0` succeeds; an injected connector raising `TimeoutError` and one raising `OSError`
each raise `CategorizedError` with `ErrorCategory.TRANSPORT_FAILURE`; a host-less URI raises
`ErrorCategory.CONFIGURATION_ERROR`); confirm they fail; implement; confirm green. Then declare the
setting with its five-part `help`, add it to `SETTINGS`, and wire `open_libvirt_reaper` to call
`require_reachable(uri, timeout=reaper_connect_timeout())` before `open_libvirt_protocol(uri)`, with
a test that a fake gate is invoked before the opener.

Acceptance: `require_reachable` never lets an unreachable endpoint reach `libvirt.open`; the failure
category is `TRANSPORT_FAILURE` so `_enter_host`'s existing isolation logs and skips the host; the
gate reads no TLS material. `just lint` and `just type` green.

## Task 2 — the lane budget

**Modifies** `src/kdive/reconciler/cleanup/provider_reaping.py`, `src/kdive/config/core_settings.py`.
**Creates** `tests/reconciler/test_reaping_budget.py`.

**Interfaces produced**:

```python
DEFAULT_LANE_BUDGET: timedelta = timedelta(seconds=10)   # provider_reaping.py
async def reap_orphaned_dump_volumes(
    conn, reaper, grace: timedelta, *, budget: timedelta
) -> int: ...
async def reap_orphaned_captures(
    conn, reapers, *, settle, batch, retry_base, retry_cap, budget: timedelta
) -> int: ...
RECONCILER_LANE_BUDGET_SECONDS: Setting                  # core_settings.py
```

Order: write the R2 test for the capture lane first — it is the load-bearing proof and the hardest
to retrofit. Its shape is fixed by the spec's *Testing* section: the fake reaper's work runs inside
`asyncio.shield`, sleeps past the budget by an order of magnitude, then probes a **second** pooled
connection with `SELECT pg_try_advisory_xact_lock(hashtextextended('kdive:job:' || %s::text, 1951))`
inside a transaction and records whether it was refused. Assert the recorded answer is "refused"
(the lane still held the fence), that the lane returned 1, and that `capture_reap_state.reclaimed_at`
is set. Confirm it fails before the budget exists. Then the dump-volume twin, probing
`_lock_key(LockScope.SYSTEM, system_id)` and satisfying all four preconditions on the locked path:
a volume name carrying a parseable System UUID, `mtime_epoch_s` older than `grace`, no live
`host_dump_volume_lease` row, no active capture job.

Then the between-candidates tests: two candidates and a budget spent by the first ⇒ exactly one
provider call and no reap-state row for the second; a budget not spent ⇒ the full batch attempted.
Then implement: a module-level `_budget_spent(deadline, lane, *, remaining)` helper logging the
unattempted count at INFO, a `time.monotonic()` deadline started at the top of each lane, and the
check at the head of each candidate loop, lexically outside every `conn.transaction()`. Update the
`_dispatch_capture` and `reap_orphaned_dump_volumes` docstrings so no comment still claims the call
is unbounded (R4). Declare `RECONCILER_LANE_BUDGET_SECONDS` with a parser that rejects a
non-positive value and a five-part `help`; add a test that `config.validate` fails on `0` and `-1`.

Mutation-verify the R2 tests: replace the between-candidates check with `asyncio.timeout` around the
provider call, run, watch both redden, restore, clear `__pycache__`, confirm green.

Acceptance: R1–R4 satisfied; `just test` green for `tests/reconciler/`.

## Task 3 — wiring and generated docs

**Modifies** `src/kdive/reconciler/loop.py`, `src/kdive/processes/reconciler.py`,
`docs/guide/reference/config.md` (generated).
**Modifies** `tests/reconciler/test_loop.py` or its neighbours where the lane signatures are called.

Add `ReconcileConfig.lane_budget: timedelta = DEFAULT_LANE_BUDGET` with a five-part docstring
comment, pass it from both `_REPAIR_CATALOG` entries (`reaped_dump_volumes`, `reaped_captures`), and
read `RECONCILER_LANE_BUDGET_SECONDS` into it in `processes/reconciler.py`'s `ReconcileConfig`
construction. Re-export `DEFAULT_LANE_BUDGET` from `loop.py` beside the other `DEFAULT_*` aliases.
Then run `just config-docs` and commit the regenerated reference.

Acceptance: `env -u FORCE_COLOR just ci` green, including `config-docs-check`, `env-docs-check`,
`config-guard`, and `adr-status-check`.

## Rollback

Every change is additive and behind a default. Reverting the branch restores the prior unbounded
behavior; no migration, no schema change, no persisted state to unwind.
