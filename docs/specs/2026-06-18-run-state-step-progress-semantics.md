# Run.state vs run_steps progress semantics on `runs.get` (#564)

Status: Draft
ADR: [ADR-0179](../adr/0179-run-state-step-progress-semantics.md)

## Problem

`RunState.SUCCEEDED` records that the **build** step finished. Install and boot progress
live in the `run_steps` ledger, and debug attach correctly requires both
`RunState.SUCCEEDED` and a succeeded `boot` step (`sessions_lifecycle.py`). The behavior
is internally guarded, but the public status *name* invites a caller to read a
`succeeded` Run as fully built **and** installed **and** booted when only the build has
completed.

Two concrete gaps for an agent driving the lifecycle over MCP:

- `runs.get` does not surface any install/boot step status, so the only progress signal a
  caller sees is `Run.state`, which says nothing past build.
- `runs.get`'s `suggested_next_actions` for a bound `SUCCEEDED` Run is always
  `["runs.get", "runs.install"]` — even after install and boot already succeeded. An agent
  that has built, installed, and booted is still pointed back at `runs.install`.

The issue (#564) is explicitly a semantics decision: do not partially rename
`RunState` without an ADR, because the status name is a public API contract.

## Decision summary

Keep `RunState` as-is (build-phase milestones; **no rename, no migration**) and make the
build-vs-install-vs-boot split explicit and discoverable on the existing discovery
surface, `runs.get`:

- Surface a per-step `data.steps` map (`build` / `install` / `boot` → step state) on a
  `SUCCEEDED` Run.
- Walk `suggested_next_actions` along the real install → boot → debug progression instead
  of always pointing at `runs.install`.
- Document the `Run.state` ⟷ `run_steps` relationship in the `RunState` docstring and the
  ADR.

## Goals

- A reader of `runs.get` on a `SUCCEEDED` Run can tell, without guessing, whether install
  and boot have happened and what the next lifecycle tool is
  (`runs.install` / `runs.boot` / `debug.start_session`).
- The intended relationship between `Run.state` and `run_steps` is recorded in an ADR and
  the source docstring: `succeeded` means build-succeeded; install/boot status comes from
  `run_steps`.
- No state machine, schema, transport, or migration change.

## Non-goals

- No rename or reshape of `RunState`. (Considered & rejected.)
- No new state surfaced for `FAILED` Runs: a failed step is terminal for the Run
  (`RunState.FAILED`) and `run_steps` never holds a `failed` row (ADR-0171), so a failure
  is already a failure envelope carrying its `error_category` (ADR-0141). `data.steps` is
  scoped to `SUCCEEDED` Runs, where the build-vs-install-vs-boot ambiguity actually exists.
- No liveness reinterpretation: a `running` step claim is read as persisted, matching
  ADR-0176. A stale `running` claim is reaped by `claim_run_step` on the next claim, not
  by this read path.
- No paging or change to `runs.list`; the `get` path is the discovery surface.

## Design

### The `Run.state` ⟷ `run_steps` relationship

One build per Run. `finalize_build` records the `run_steps(step='build', state='succeeded')`
row and drives `running → succeeded` in the same per-Run transaction, so a `build` row
exists **iff** the Run is `SUCCEEDED` (or beyond, but `SUCCEEDED` is terminal for the build
phase). Install and boot are separate idempotent step jobs admitted *after* the Run is
already `succeeded`; each is a `run_steps` row that goes `running → succeeded` via
`claim_run_step` / `complete_run_step`, or is abandoned (row deleted) on failure. A failed
install/boot step fails the Run to `FAILED`; `run_steps` therefore only ever holds
`running` or `succeeded` (ADR-0171).

So `Run.state.succeeded` is precisely "build succeeded"; install and boot are answered only
by `run_steps`.

### `data.steps` on `runs.get`

For a `SUCCEEDED` Run, `runs.get` adds a fixed-key `data.steps` map:

```json
"steps": { "build": "succeeded", "install": "succeeded", "boot": "running" }
```

- Keys are always exactly `build`, `install`, `boot` (no guessing which keys exist).
- Value is the persisted `run_steps.state` (`succeeded` / `running`) or `pending` when no
  row exists yet (step not started). `pending` is a read-surface synthesis, not a persisted
  state; the persisted vocabulary stays `{running, succeeded}` (ADR-0171).
- `build` is always `succeeded` on a `SUCCEEDED` Run (the row exists by construction).
- The map is **absent** on non-`SUCCEEDED` Runs: `CREATED`/`RUNNING` are pre-build,
  `CANCELED` is terminal, `FAILED` is a failure envelope.

One extra read on the `get` path: a single
`SELECT step, state, result FROM run_steps WHERE run_id = %s AND step IN ('build','install','boot')`.
The `result` is read so the `boot` row's recorded `boot_outcome` can drive the booted-run
next-action (below); the `steps` map itself only uses `state`. `runs.list` is unchanged (no
N+1).

### `suggested_next_actions` progression

For a `SUCCEEDED` Run, the second action walks the real progression (the first stays
`runs.get`):

| Condition | Second action(s) |
|---|---|
| unbound (`system_id is None`) | `runs.bind` |
| bound, `install` not `succeeded` | `runs.install` |
| bound, `install` succeeded, `boot` not `succeeded` | `runs.boot` |
| bound, `install` + `boot` succeeded, boot `boot_outcome == "expected_crash_observed"` | `postmortem.triage`, `vmcore.fetch` |
| bound, `install` + `boot` succeeded, boot booted normally | `debug.start_session` |

The booted-run branch keys on the **observed** `boot_outcome` recorded in the `boot` step
result, not on the Run's create-time `expected_boot_failure` field. The two diverge: a Run
that set `expected_boot_failure` but then booted normally records `boot_outcome: "ready"`
(`runs_boot.py`) and **is** live-debuggable, so it is routed to `debug.start_session`; only
a boot that actually crashed as expected (`boot_outcome == "expected_crash_observed"`) is
routed to crash triage. The `postmortem.triage` / `vmcore.fetch` pair matches the failure
envelope `sessions_lifecycle.py` itself returns when a live attach is attempted on an
`expected_crash_observed` boot, so the two agent-facing surfaces agree. The `data.steps`
map remains the ground truth regardless of the recommended action; a `running` install/boot
still recommends the same forward tool (the step jobs are idempotent), and the caller can
read `steps` to see an in-flight claim.

### Documentation

- The `RunState` docstring states that `succeeded` is build-succeeded and that
  install/boot status lives in `run_steps` / is surfaced by `runs.get`.
- The `runs.get` tool description and the generated reference note that `succeeded` means
  build-succeeded and that `data.steps` carries install/boot progress.

## Acceptance criteria

- A `SUCCEEDED` Run's `runs.get` carries `data.steps` with `build`/`install`/`boot` keys
  reflecting `succeeded` / `running` / `pending`.
- A non-`SUCCEEDED` Run carries no `data.steps`.
- `suggested_next_actions` walks unbound→bind, built→install, installed→boot,
  booted-normally→debug.start_session, and crashed-as-expected→postmortem.triage/vmcore.fetch.
- The `RunState` docstring and `runs.get` docs state that `succeeded` is build-succeeded.
- Tests pin each `steps`/next-action case (built-only, install-running, installed,
  booted-normally, booted-with-`expected_crash_observed`, and an
  `expected_boot_failure` Run that booted normally → debug.start_session, unbound).

## Considered & rejected

- **Rename/reshape `RunState`** (e.g. `built` instead of `succeeded`, or a composite
  build/install/boot status). Rejected here: the issue forbids a partial rename without a
  separate ADR; the name is a persisted, public contract (DB CHECK, envelopes, every
  caller). The discoverability problem is solved additively without breaking the contract.
- **Surface only refined `suggested_next_actions`, no `steps` map.** Rejected: the agent
  would infer step state purely from the recommended action, which is weaker for criterion
  3 ("expose enough step status … without guessing") and gives no signal for an in-flight
  `running` step.
- **A single derived `phase` enum** (`built`/`installed`/`booted`). Rejected: it
  introduces a new vocabulary, collapses the `running` vs `succeeded` distinction, and is
  redundant with the explicit per-step map.
- **Reinterpret a stale `running` claim as failed on read.** Rejected: read paths report
  persisted state (ADR-0176); `claim_run_step` owns staleness reaping.
