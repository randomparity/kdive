# Spec — Per-Run vmcore capture (#796)

- **Issue:** [#796](https://github.com/randomparity/kdive/issues/796) (split from #781; epic #764)
- **ADR:** [ADR-0244](../../adr/0244-per-run-vmcore-capture.md) (supersedes
  [ADR-0050](../../adr/0050-vmcore-method-aware-storage.md))
- **Status:** Approved for implementation

## Problem

Raw vmcores are stored and owned per System (`…/systems/{system_id}/vmcore-{method}`, ADR-0050),
not per the Run that crashed. The capture job has no `run_id`: `vmcore.fetch(system_id, method)` is
System-addressed and `CaptureVmcorePayload` carries no `run_id`, so a core cannot be attributed to
the crashing Run. #781's egress had to carry that indirection
(`run.system_id → raw_vmcore_key(system_id)`) with a note that it would address by `run_id` directly
once per-Run capture landed.

**Reachability.** The issue frames this as "a reused System crashing under a later Run loses the
second core." Under the current System state machine that case is not yet reachable: `CRASHED`
transitions only to `TORN_DOWN`/`FAILED` (`src/kdive/domain/capacity/state.py`), with no edge back
to `READY`/`REPROVISIONING`, so a `system_id` reaches `CRASHED` — and produces a vmcore — at most
once. Per-System keying does not drop a core today. The reachable value of this change is correct
Run attribution (so #781 resolves by `run_id` with no System indirection) plus forward-compatibility
if a later ADR makes a `CRASHED` System reprovisionable.

## Goal

Make raw vmcore capture **Run-addressed** and cores **Run-owned**: `vmcore.fetch(run_id, method)`
writes a core owned by the Run (`owner_kind='runs'`). The captured core is attributed to its Run;
should a System ever crash under successive Runs, each retains its own core. The #781 egress
resolves the per-Run core by `run_id` directly. Capture idempotency stays correct under concurrent
same-Run `vmcore.fetch`.

## Non-goals

- Per-method multiplicity within a single Run (one core per Run, first-method-wins — ADR-0244 §3).
- Any change to the capture *mechanics* (libguestfs harvest, libvirt core dump, guest-agent kdump).
- A migration/backfill of existing per-System cores (none persisted in M0/M1).
- A vmcore delete/replace affordance (out of scope, as in ADR-0050).

## Acceptance criteria

1. A core captured under a Run is owned by that Run (`owner_kind='runs'`, `owner_id={run_id}`) and
   resolvable by `run_id`; two Runs that each capture a core (e.g. distinct Runs on distinct
   Systems, or — once reachable — successive Runs on one reused System) retain distinct cores at
   distinct keys, neither shadowing the other. (Two crashes on a single `system_id` are not
   reachable under the current state machine — `CRASHED` is terminal, `state.py` — so this is
   asserted at the artifact-ownership level, exercised by inserting two Run-owned cores, not by an
   end-to-end double-crash run.)
2. `vmcore.fetch(run_id, method)` admits a `capture_vmcore` job on a Run whose bound System is
   `CRASHED`; it rejects (no job row) for a malformed/absent/cross-project Run (`not_found` /
   `configuration_error`), a Run not bound to a System, and a non-`CRASHED` System — with the same
   typed envelopes the System-addressed tool produced for the state checks.
3. A second `vmcore.fetch` for the **same Run** with a **different** core method is refused with
   `configuration_error` naming both methods (first-method-wins, per Run).
4. Concurrent `vmcore.fetch` for the **same Run + method** produces exactly one core and one job;
   the per-Run advisory lock + dedup key (`{run_id}:capture_vmcore:{method}`) serialize the race
   (adversarial test).
5. A keyed retry (`idempotency_key`) replays the identical job envelope (ADR-0193, unchanged).
6. `artifacts.fetch_raw(run_id, "vmcore")` resolves the Run's own core (`raw_vmcore_key(run_id)`),
   gated on `run.project`, with no `run.system_id → raw_vmcore_key` indirection.
7. `postmortem.crash`/`.triage` and `introspect.from_vmcore` resolve the Run's own core via the
   per-Run lookup; their tool surface is unchanged.
8. The generated tool reference (`docs/guide/reference/vmcore.md`) reflects the `run_id` argument.

## Design

### Addressing & ownership (ADR-0244 §1, §2)

- `vmcore.fetch(run_id, method)`: load Run → `system = SYSTEMS.get(run.require_system_id())` →
  require `CRASHED` → `contributor` on `run.project` → enqueue
  `CaptureVmcorePayload(run_id, method)` with dedup `{run_id}:capture_vmcore:{method}`.
- Object key `…/runs/{run_id}/vmcore-{method}` (+ `-redacted`). `raw_vmcore_key` resolves by
  `owner_kind='runs' AND owner_id={run_id}`.

### Capture port (ADR-0244 §5)

`Retriever.capture(system_id, run_id, method)`. `system_id` locates the live domain/overlay/volume
(mechanics unchanged); `run_id` sets `owner_kind='runs'`, `owner_id={run_id}` in every put/key in
the three providers. `CaptureOutput` is unchanged (it already carries the `StoredArtifact`s with the
baked-in key).

### Worker handler (ADR-0244 §4, §7)

`CaptureVmcorePayload(run_id, method)` (RunPayload base). Handler: load Run + bound System under
`LockScope.RUN`; `precheck` returns an existing per-Run core or the `(system_id, run_id)` to capture;
`capture(system_id, run_id, method)` runs the slow seam unlocked; `finalize` re-checks the per-Run
core (race backstop), inserts both rows `owner_kind='runs'`, and audits `object_kind='runs'`,
`object_id={run_id}` with `run.project`. `CAPTURE_VMCORE` registered run-bearing in
`run_id_from_payload`.

### Egress & readers (ADR-0244 §6, §3)

- `raw_fetch._resolve_key` `vmcore` branch: `require_role(ctx, run.project, CONTRIBUTOR)` +
  `raw_vmcore_key(run_id)`; remove the `run.system_id`/`system_project` use on this path. Gating on
  `run.project` preserves cross-project isolation because a bound Run always shares its System's
  project — enforced at `services/runs/admission.py` (`system.project != inv.project` → reject) and
  `services/runs/bind.py` (`system.project != run.project` → reject). Add a test asserting
  `fetch_raw('vmcore')` is denied for a Run whose System is cross-project (guards a future bind
  regression). If `RunFetchContext.system_id` becomes unused after this, remove it (no dead code).
- `_vmcore_targets.resolve_run_vmcore_target`: `raw_vmcore_key(run_id)` (was
  `raw_vmcore_key(run.require_system_id())`).

## Failure modes & edges (test these)

- Malformed `run_id` → `configuration_error` (parse), no job row.
- Absent / cross-project Run → `not_found` (no membership leak), no job row.
- Run with `system_id is None` (unbound) → `configuration_error` (cannot capture), no job row.
- System not `CRASHED` → `configuration_error` with `current_status`, no job row.
- Same Run, second different method → `configuration_error` (both methods named), at `precheck` and
  the `finalize` backstop.
- Concurrent same-Run/method → one core, one job (per-Run lock + dedup).
- Two Run-owned cores (distinct Runs on distinct Systems, or two cores inserted directly) → distinct
  keys, each resolvable by its `run_id`, neither shadowing the other (artifact-ownership level; two
  crashes on one `system_id` are not reachable — see AC#1).
- `fetch_raw` for a Run with no captured core → `configuration_error` `vmcore_unavailable`.

## Rollback

Pure code + docs change, no schema/data migration. Revert the branch; no persisted state to
unwind (the per-Run object keys are new; no production cores exist at per-System keys to restore).

## Files touched

- `src/kdive/jobs/payloads.py` — `CaptureVmcorePayload` → RunPayload; register run-bearing.
- `src/kdive/jobs/handlers/vmcore.py` — Run-addressed precheck/finalize, per-Run lock, audit.
- `src/kdive/db/artifact_queries.py` — `raw_vmcore_key(run_id)` per-Run lookup.
- `src/kdive/providers/ports/retrieve.py` — `capture(system_id, run_id, method)`.
- `src/kdive/providers/{local_libvirt,fault_inject}/retrieve.py`,
  `src/kdive/providers/remote_libvirt/retrieve/{facade,kdump_capture,host_dump_capture,common}.py` —
  thread `run_id`; `owner_kind='runs'`.
- `src/kdive/mcp/tools/lifecycle/vmcore.py` — `vmcore.fetch(run_id, …)` admission.
- `src/kdive/mcp/tools/_vmcore_targets.py` — per-Run resolution.
- `src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py` — Run-keyed `vmcore` egress.
- `docs/guide/reference/vmcore.md` — regenerated.
- Tests: `tests/mcp/lifecycle/test_vmcore_tools.py`, `tests/mcp/test_vmcore_targets.py`,
  `tests/db/test_artifact_queries.py`, `tests/jobs/test_payloads.py`, the three providers'
  retrieve tests, the `fetch_raw` egress tests, and a `tests/adversarial/` same-Run concurrency test.
