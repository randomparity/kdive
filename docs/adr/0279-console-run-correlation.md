# ADR-0279: Run-correlated console artifacts and a Run-scoped console manifest

- Status: Accepted
- Issue: #935
- Spec: [console-run-correlation-935](../specs/2026-06-30-console-run-correlation-935.md)
- Supersedes nothing; extends ADR-0235 (per-Run console evidence snapshot), ADR-0273 (rotating
  System-owned console parts), ADR-0262 (`runs.get` `data.console_access` read-path hint), ADR-0244
  (Run-owned vmcore — the contrast this decision deliberately does *not* follow for console). Builds
  on the agent-facing-contract rule (CLAUDE.md: the `@app.tool` wrapper docstring is what the agent
  reads).

## Context

Console artifacts are stored System-owned with no per-Run correlation. Both families are
`owner_kind='systems'`, `owner_id=<system_id>`: the immutable per-Run boot-evidence snapshot
`console-<run_id>` (ADR-0235) and the rotating post-readiness parts `console-part-<gen>-<index>`
(ADR-0273). `artifacts.list(system_id)` is System-scoped only — it returns every redacted artifact
for the System, mixing all Runs and sessions — and `runs.get` surfaces only the boot step's single
`console_evidence_artifact_id` as `refs.console`, never the rotating parts. After several
reprovision/build/test cycles on one System an agent cannot tell which console bytes belong to which
Run, so durable failure evidence (e.g. a KASAN report seen live) is not locatable afterward. The
black-box review calls this the largest verification gap.

Console was **deliberately** left System-owned. Rotation is keyed on **System** liveness, not Run
terminality (ADR-0273): a `ready` System keeps emitting console after its hosting Run has
`succeeded`, and a terminal Run must not stop capture. Console parts are reclaimed at System
teardown and are excluded from the run-owned artifact-expiry sweep (#768,
`reconciler/cleanup/gc.py`). vmcore, by contrast, is genuinely Run-scoped and was made Run-owned
(ADR-0244). So console cannot simply be re-owned to Runs the way vmcore was.

Attribution is non-trivial because the worker that seals parts (`console_rotate`, reconciler-
dispatched) knows only `system_id` and `boot_id` — not a Run — and the Run that booted the System is
already in `SUCCEEDED` state by the time parts are sealed (build done; install/boot are `run_steps`,
ADR-0179). The domain's `RUN_NON_TERMINAL` set (`{CREATED, RUNNING}`) therefore never contains a
booted Run and cannot identify the "active" Run.

There is also a discoverability dimension: the agent reads only the wrapper docstring + `Field`
text. `runs.get`'s wrapper is silent on `refs.console`/`data.console_access`, and `artifacts.list`'s
is "List the redacted artifacts for a System. Requires viewer." — it never says the listing is
System-scoped or documents the console key naming, so even existing correlation data is invisible.

## Decision

Add a nullable **correlation** column and a Run-scoped read surface; keep console ownership on the
System.

1. **`artifacts.run_id`, a correlation attribute orthogonal to ownership.** Migration 0054 adds a
   nullable `run_id uuid REFERENCES runs (id)` to `artifacts` plus a partial index
   `WHERE run_id IS NOT NULL`. A console artifact stays `owner_kind='systems'` (teardown reclaim and
   the #768 expiry exclusion unchanged) and additionally records the id of the Run active during its
   window. NULL means "uncorrelated" — the historical default and the graceful-degradation value.
   `run_id` is added to the `Artifact` domain model with a `None` default, so the generic
   metadata-driven `Repository.insert` writes it automatically and every non-console insert writes
   NULL with no call-site change.

2. **Exact attribution for the boot-evidence snapshot.** The boot worker already holds the `run_id`
   when it writes `console-<run_id>`, so it stamps that id directly.

3. **Per-job, lock-held attribution for rotating parts.** A `console_rotate` job, once per job and
   inside the per-System advisory lock it already holds, resolves the System's **most-recently-booted
   Run** (the Run bound to the System whose `boot` `run_steps` row is the most recent) and stamps that
   id onto every part the job seals. This is race-free: a part is sealed only while the System is live
   (reached only via a successful boot that wrote the `boot` step), and a power-cycle by a *new* Run
   changes `boot_id` — which resets rotation to a fresh generation and serializes on the same per-
   System lock — so within one job's lock-held section the current boot, and thus the most-recently-
   booted Run, is stable. No straggler old-generation part can be misattributed. When no `boot` step
   resolves, parts are stamped NULL, never wrongly attributed.

4. **A bounded, ordered Run-scoped manifest on `runs.get`.** `runs.get` surfaces
   `data.console_artifacts`: a newest-first list (mirroring `artifacts.list`'s `created_at DESC`) of
   `{artifact_id, object_key, created_at}` for the console artifacts where `run_id = <run>` and
   `owner_kind='systems'`, bounded to `CONSOLE_MANIFEST_MAX` (100) with `data.console_artifacts_total`
   and `data.console_artifacts_truncated` when the cap is exceeded. The manifest includes the boot-
   evidence snapshot and every attributed part; the key is omitted when the Run has none, so an
   uncorrelated or pre-migration Run reads as today. `refs.console` and `data.console_access` are
   unchanged and additive to this.

5. **Make the contract discoverable.** Update the `@app.tool` wrapper docstrings: `runs.get` names
   `refs.console`, `data.console_access`, and `data.console_artifacts`; `artifacts.list` states it is
   System-scoped (mixes all Runs/sessions), documents the `console-<run_id>` vs
   `console-part-<gen>-<index>` naming, and points at the `runs.get` manifest for Run correlation.

No new MCP tool, parameter, RBAC role, error category, config setting, or destructive job kind. The
only schema change is the additive migration 0054 (forward-only, ADR-0015). No backfill: rows
written before the change keep `run_id = NULL`.

## Consequences

- An agent reads "all durable console evidence for this Run" from `runs.get data.console_artifacts`
  — an ordered index of artifact ids it then pages with `artifacts.get` / searches with
  `artifacts.search_text` — instead of guessing which `console-part-*` rows in a System-scoped list
  belong to its Run. The largest black-box verification gap closes for forward evidence.
- Ownership and lifecycle are untouched: console stays System-owned, reclaimed at teardown, excluded
  from the run-owned expiry sweep. The new column is purely additive metadata.
- Attribution is honest about uncertainty: a Run with no resolvable boot, or any pre-migration
  artifact, reads `run_id = NULL` and contributes no manifest entry rather than a guessed Run.
- A chatty multi-hour Run can correlate many parts; the manifest is bounded and discloses its total
  and truncation, so `runs.get` stays token-bounded while the full set stays reachable via
  `artifacts.get`/`artifacts.list`.
- The `run_id` FK means a console artifact cannot name a non-existent Run; a future console-delete
  path must clear or cascade `run_id` (teardown reclaim already removes the rows, so nothing strands
  today).

## Considered & rejected

- **Re-own console artifacts to Runs (the vmcore/ADR-0244 model).** Rejected: rotation is keyed on
  System liveness across a Run's terminality (ADR-0273), console is reclaimed at System teardown and
  excluded from the run-owned expiry sweep, and a System outlives its Runs. Re-owning would break
  that lifecycle. Correlation without re-ownership preserves both.
- **Attribute via `RUN_NON_TERMINAL` ("the active Run").** Rejected: a booted Run is in `SUCCEEDED`
  state (build done; install/boot are `run_steps`, ADR-0179), so `{CREATED, RUNNING}` never contains
  it. The most-recently-*booted* Run is the correct signal.
- **Per-part run-id resolution (resolve at each `_seal_part`).** Rejected as redundant: the boot is
  stable for the whole lock-held job, so one resolution per job is sufficient and cheaper, and a per-
  part query buys no extra correctness.
- **Persist a `boot_id → run_id` map (new table or sidecar field).** Rejected as unnecessary state:
  the most-recently-booted-Run query over existing `run_steps` is race-free under the per-System lock
  and needs no new mapping to maintain or reclaim.
- **A new Run-scoped `artifacts.list` variant / a new MCP tool.** Rejected: the platform already has
  too many tools (ADR-0273's stance); the manifest rides on `runs.get`, which the agent already calls
  to drive the Run lifecycle, and the per-artifact `artifacts.get`/`search_text` reads are unchanged.
- **Backfill historical console artifacts.** Rejected: the value is durable forward evidence, and a
  heuristic backfill of System-scoped history would invent attributions a NULL states honestly.
- **An unbounded manifest.** Rejected: a chatty long-lived Run would blow the `runs.get` token
  budget; the bound + total/truncated disclosure keeps the read token-safe (the same posture as
  ADR-0257's `artifacts.get` window cap).
