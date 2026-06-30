# Spec: Run-correlated console artifacts and a Run-scoped console manifest (#935)

- Issue: #935
- ADR: [ADR-0279](../adr/0279-console-run-correlation.md)
- Status: Draft

## Problem

Console artifacts are stored **System-owned** with no per-Run correlation, and `runs.get` exposes
only the single boot-console reference. After several reprovision/build/test cycles on one System,
an agent cannot reliably tell which console bytes belong to which Run, so the console is an
unreliable place to find durable failure evidence (e.g. a KASAN report seen live but not locatable
afterward). This is the largest verification gap from the black-box review (`BLACK_BOX_REVIEW.md`
§2/§3).

Two facts shape the gap:

- **Ownership.** Both console artifact families are `owner_kind='systems'`, `owner_id=<system_id>`:
  the immutable per-Run boot-evidence snapshot `console-<run_id>` (ADR-0235,
  `jobs/handlers/runs/boot_evidence.py`) and the rotating post-readiness parts
  `console-part-<gen>-<index>` (ADR-0273, `jobs/handlers/console_rotate.py`). The object key of the
  boot-evidence snapshot embeds the run id; the rotating parts carry no Run attribution at all.
- **No Run-scoped read.** `artifacts.list(system_id)` is System-scoped only (it returns every
  redacted artifact for the System, mixing all Runs and sessions —
  `services/artifacts/listing.py`), and `runs.get` surfaces only the boot step's single
  `console_evidence_artifact_id` as `refs.console` (`mcp/tools/lifecycle/runs/common.py`,
  `view.py`), never the rotating parts.

A second, independent dimension is **discoverability**. The agent reads only the `@app.tool`
wrapper docstring and `Field` text (CLAUDE.md "wrapper docstring is the agent-facing contract").
Today `runs.get`'s wrapper docstring is silent on `refs.console` and `data.console_access`, and
`artifacts.list`'s wrapper is just "List the redacted artifacts for a System. Requires viewer." —
it does not say the listing is System-scoped, does not document the `console-<run_id>` vs
`console-part-<gen>-<index>` naming, and gives no path to correlate a console artifact back to a
Run. So an agent cannot correlate even with the data that exists.

## Goal

1. **Correlate** every console artifact (boot-evidence snapshot and rotating parts) to the Run
   active during its window, via a nullable `artifacts.run_id` attribution column that is
   **distinct from ownership** (ownership stays `systems`, so teardown reclaim and the
   console/vmcore expiry exclusion of ADR-0273/#768 are unchanged).
2. **Expose** an ordered, bounded, Run-scoped console manifest on `runs.get`
   (`data.console_artifacts`: each entry an artifact id, object key, and creation timestamp), so an
   agent reads "all console evidence for this Run" from the envelope, not by guessing keys.
3. **Document** the console surface in the agent-facing wrapper docstrings of `runs.get` and
   `artifacts.list` so the correlation is discoverable at call time.

## Non-goals

- **Re-owning console artifacts to Runs.** Ownership stays `systems`. Console rotation is keyed on
  **System** liveness, not Run terminality (ADR-0273), and a System outlives its Runs; re-owning
  would break the teardown-reclaim lifecycle and the #768 expiry exclusion. `run_id` is a
  correlation attribute, not an ownership change. (Contrast vmcore, which is genuinely Run-scoped
  and Run-owned, ADR-0244.)
- **Backfilling** existing console artifacts. Rows written before this change keep `run_id = NULL`
  (uncorrelated, exactly as today). The fix is forward-looking durable evidence.
- **A new MCP tool, RBAC role, error category, config setting, or destructive gate.** The manifest
  is an additive `data` field on `runs.get`; correlation is one additive nullable column.
- **A System-spanning console search or `artifacts.list` pagination.** Out of scope here (named as
  future work in ADR-0273's R8a).
- **Changing the byte content, redaction, or object keys of any console artifact.** Only the
  attribution column and the read surface change.

## Requirements

R1. **Attribution column.** Migration 0054 adds a nullable `run_id uuid REFERENCES runs (id)`
column to `artifacts`, plus a partial index `WHERE run_id IS NOT NULL` for the manifest query. The
column is **correlation**, orthogonal to `(owner_kind, owner_id)` ownership: a console artifact
stays `owner_kind='systems'` and additionally carries the id of the Run active during its window.
NULL means "uncorrelated" (the historical default and the graceful-degradation value).

R2. **Boot-evidence attribution (exact).** When the boot worker captures the per-Run snapshot
`console-<run_id>` it already holds the `run_id`, so it stamps `run_id` directly. This is exact, not
heuristic.

R3. **Rotating-part attribution (per-job, under the lock).** A `console_rotate` job seals zero or
more parts under the per-System advisory lock. Once per job, **inside that lock**, it resolves the
System's **most-recently-booted Run** — the Run bound to this `system_id` that has a `boot`
`run_steps` row, taking the most-recently *created* such Run (R-query below) — and stamps that
`run_id` onto every part the job seals. The resolution is stable within a job because:

- A part is sealed only while the System is live (`ready`/`crashed`), which is reached only via a
  successful boot that wrote a `boot` `run_steps` row, so the most-recently-booted Run is the Run
  that produced the current boot.
- A power-cycle by a *new* Run normally changes `boot_id`, which resets the rotation to a fresh
  generation (ADR-0273 R6b) and drops the old generation's unsealed carry; and power-cycles
  serialize on the same per-System lock this job holds. So within one job's lock-held section the
  current boot — and therefore the most-recently-booted Run — does not move under the job.

**Residual coarseness (inherited from ADR-0273 R6b).** This is *not* misattribution-proof in one
inherited edge: the reconciler degrades `boot_id` to `""` when it cannot `os.stat` the console file
(non-co-located/unreadable, `console_rotation.py:_boot_id`). If a new Run's power-cycle leaves
`boot_id` unchanged (`""` → `""`) **and** the new console file regrew past the old plaintext offset
before a sweep observed a shrink, ADR-0273 R6b already does not detect the new boot — it mislabels
the new boot's bytes as a continuation of the old generation, and the held-back seam `carry`
(≤ `SEAM_OVERLAP` bytes) of the old Run is emitted into a part the job now resolves to the *new*
most-recently-booted Run. The console **byte stream** is already cross-contaminated at that seam by
R6b; the `run_id` attribution is no worse than that pre-existing limitation, and it is bounded to the
single straddling part. We accept this residual rather than add per-generation sidecar state (see
ADR-0279 rejected alternatives); it is the same missed-power-cycle case ADR-0273 already documents,
not a new failure surface. The claim is therefore "stable under the lock, with the ADR-0273 R6b
missed-power-cycle residual," not "misattribution impossible."

When no `boot` step resolves (e.g. a boot step deleted by the ADR-0185 terminal-failure recycle on
a System the reconciler has not yet torn down), the parts are stamped `run_id = NULL` — uncorrelated,
never wrongly attributed. The booting Run is in `SUCCEEDED` state by this point (build completed;
install/boot are `run_steps`, ADR-0179), so attribution must **not** be derived from
`RUN_NON_TERMINAL` (`{CREATED, RUNNING}`) — that set never contains a booted Run.

R4. **Best-effort, never failing capture.** Attribution resolution is part of the existing
best-effort rotation/boot-evidence paths (ADR-0273 R7, ADR-0235). A resolution query failure logs
once and degrades to `run_id = NULL`; it never fails the rotation job, the boot, or a tool call.
The capture's existing degrade-to-no-parts behavior on store/permission walls is unchanged.

R5. **Run-scoped console manifest on `runs.get`.** `runs.get` surfaces `data.console_artifacts`: an
ordered list of `{artifact_id, object_key, created_at}` for the console artifacts correlated to this
Run — `SELECT id, object_key, created_at FROM artifacts WHERE run_id = <run> AND
owner_kind='systems' AND sensitivity='redacted'`. The list is **newest-first**, ordered
`(created_at DESC, object_key DESC)` — a **total** order, because every part a single
`console_rotate` job seals commits in one transaction and so shares an identical `created_at`
(`now()` is the transaction timestamp); the zero-padded `console-part-<gen>-<index>` key is the
within-batch tiebreak (the same deterministic rule ADR-0273 R8 uses for tail identification), so the
manifest is not ambiguous within a same-second batch. It is **bounded** to a fixed
`CONSOLE_MANIFEST_MAX` (100). When more correlated console artifacts exist than the cap,
`data.console_artifacts_total` carries the full count and `data.console_artifacts_truncated` is
`true`; the returned entries are the newest `CONSOLE_MANIFEST_MAX`. The manifest includes the
boot-evidence snapshot (`console-<run_id>`, now stamped under R2) and every attributed part. It is
omitted entirely (no key) when the Run has no correlated console artifacts, so an uncorrelated or
pre-migration Run reads exactly as today.

R5a. **Truncation drops the oldest entries — disclosed.** Because the manifest is newest-first, a
Run with more than `CONSOLE_MANIFEST_MAX` correlated console artifacts omits its **oldest** parts
from the returned list (`_total`/`_truncated` disclose that this happened). A crash signature that
occurred early in a very long (> ~6 MiB console) Run can therefore fall outside the returned
window. The boot-evidence snapshot (`console-<run_id>`) — which holds the boot-window console
including a boot-time crash — is always the oldest entry and is dropped first under truncation, so an
agent that needs the boot console under truncation reads `refs.console` directly (it is unchanged,
R6). For an early *post-readiness* event under truncation, the entry is reachable by paging the
correlated set: the agent has the newest 100 ids here, and older correlated parts are found via
`artifacts.list(system_id)` filtered to the `console-part-*` keys (System-scoped, so it still mixes
Runs — the residual the no-run-scoped-list ADR rejection leaves open, named as future work, not
solved here). Newest-first is chosen because the live-tail and most-recent-crash cases are the
common ones; the truncation blind spot is for the > 6 MiB-per-Run tail only.

R6. **Existing console surface preserved.** `refs.console` (the boot-evidence artifact id) and
`data.console_access` (ADR-0262 read-path hint) on `runs.get` are unchanged. The manifest is
additive; an agent that only reads `refs.console` sees no behavior change.

R7. **Discoverable agent-facing contract.** The `@app.tool` wrapper docstrings (and `Field` text
where relevant) are updated so the agent reads the contract at call time (CLAUDE.md):

- `runs.get`: names `refs.console` (boot-window snapshot), `data.console_access` (how to read it),
  and the new `data.console_artifacts` Run-scoped manifest (with its bound and truncation keys).
- `artifacts.list`: states the listing is **System-scoped** (mixes every Run and session on the
  System), documents the `console-<run_id>` (per-Run boot snapshot) vs
  `console-part-<gen>-<index>` (rotating post-readiness parts) naming, and points at `runs.get`'s
  `data.console_artifacts` for Run correlation.

R8. **One additive migration; no RBAC/tool-surface/config/destructive change.** The only schema
change is migration 0054 (nullable column + partial index, forward-only, ADR-0015). No new tool, no
new MCP parameter, no RBAC role, no error category, no config setting, no destructive job kind.

## Approach

### Data model (`db/schema/0054_*.sql`, `domain/catalog/artifacts.py`, `artifacts/registration.py`)

Add `run_id uuid REFERENCES runs (id)` (nullable) to `artifacts` and a partial index
`artifacts_run_id_idx ON artifacts (run_id) WHERE run_id IS NOT NULL`. Add `run_id: UUID | None =
None` to the `Artifact` domain model; the generic `Repository.insert` derives INSERT columns from
`model_fields`, so the new column is written automatically (NULL by default). `register_artifact_row`
gains a `run_id: UUID | None = None` keyword so only the two console paths pass a value; every other
caller is unchanged and writes NULL.

### Boot-evidence attribution (`jobs/handlers/runs/boot_evidence.py`)

`_upsert_console_artifact_row` already receives `run_id`. Pass it into `register_artifact_row(...,
run_id=run_id)`. On the idempotent re-capture path (`existing is not None`), a row created
post-migration already carries the correct `run_id` (same Run), so no update is needed; a row first
written **before** migration 0054 keeps its `run_id = NULL` through re-capture — that straddle case
is covered by the no-backfill non-goal (a pre-migration artifact stays uncorrelated, not silently
healed), not a defect.

### Rotating-part attribution (`jobs/handlers/console_rotate.py`)

`_rotate_under_lock` already runs under the per-System advisory lock and has the live `conn`. After
confirming the System is live, resolve the most-recently-booted Run once
(`services/runs/steps.py` helper `latest_booted_run_id(conn, system_id)`), then thread that
`run_id` into `_seal_part` → `register_artifact_row(..., run_id=run_id)`. The resolution is a single
indexed query; a failure logs once and yields `None` (parts stamped NULL).

### Run-scoped manifest (`mcp/tools/lifecycle/runs/`)

A new query (`services/artifacts/listing.py` `list_run_console_artifacts(conn, run_id, limit)`)
returns the newest `CONSOLE_MANIFEST_MAX + 1` correlated console rows (to detect truncation) plus a
cheap `count(*)`. `get_run` (`view.py`) calls it on the success path and passes the result into
`envelope_for_run`, which renders `data.console_artifacts` / `_total` / `_truncated`
(`common.py`). The manifest entries are project-scoped by construction (the Run is already
project-checked), so the ids carry no cross-project signal — the same argument
`active_debug_session_ids` already relies on.

### Resolution query (`services/runs/steps.py`)

```sql
SELECT r.id
FROM runs r
JOIN run_steps st ON st.run_id = r.id AND st.step = 'boot'
WHERE r.system_id = %s
ORDER BY r.created_at DESC
LIMIT 1
```

Returns the most-recently-booted Run for the System, or `None`. Ordering is on the **immutable**
`runs.created_at`, not `run_steps.updated_at`: `run_steps` carries a `set_updated_at` trigger that
bumps `updated_at` on any row mutation, so ordering by it would let an incidental later touch of an
older Run's boot step invert the result. A System hosts Runs sequentially (admission allows at most
one `CREATED`/`RUNNING` Run per System), and a Run is created before it boots, so among Runs that
have a `boot` step (the JOIN), the most-recently *created* one is the one whose boot is current. A
freshly `CREATED` Run that has not yet booted has no `boot` step and is excluded by the JOIN, so it
cannot win over the Run whose boot is actually live.

## Acceptance criteria

- `artifacts` has a nullable `run_id uuid REFERENCES runs (id)` with a partial index; existing rows
  and every non-console insert write `run_id = NULL`. (R1, R8)
- The per-Run boot-evidence snapshot `console-<run_id>` is written with `run_id` = that Run, exactly
  (not heuristically). (R2)
- A `console_rotate` job stamps every part it seals with the System's most-recently-booted Run,
  resolved once under the per-System lock; a System whose most recent boot belongs to Run R attributes
  its parts to R even though R is in `SUCCEEDED` state. A System with no resolvable `boot` step
  stamps `run_id = NULL` and never raises. (R3, R4)
- Rotation continues to attribute correctly across a reprovision: parts sealed while Run R1's boot
  is current attribute to R1; after a power-cycle by Run R2 (new `boot_id`, new generation) the new
  parts attribute to R2; no part is attributed to both. (R3)
- `runs.get` returns `data.console_artifacts` as a newest-first list of `{artifact_id, object_key,
  created_at}` for the Run's correlated console artifacts, including the boot-evidence snapshot and
  every attributed part; the key is omitted when the Run has none. (R5)
- The manifest order is a **total** order `(created_at DESC, object_key DESC)`: two parts sealed in
  the same `console_rotate` job share an identical `created_at` (one transaction's `now()`) and are
  ordered deterministically by their `console-part-<gen>-<index>` key, so a same-second batch is not
  ambiguous. (R5)
- When more than `CONSOLE_MANIFEST_MAX` (100) console artifacts are correlated, the manifest returns
  the newest 100, `data.console_artifacts_total` is the full count, and
  `data.console_artifacts_truncated` is `true`; the dropped entries are the **oldest**, and the
  boot-evidence snapshot (always reachable via `refs.console`) is among the first dropped. (R5, R5a)
- `refs.console` and `data.console_access` on `runs.get` are byte-identical before and after this
  change; a Run with one boot-evidence artifact and no parts reads as today plus a one-entry
  manifest. (R6)
- The `runs.get` wrapper docstring names `refs.console`, `data.console_access`, and
  `data.console_artifacts`; the `artifacts.list` wrapper documents the System-scoped nature, the
  `console-<run_id>` vs `console-part-<gen>-<index>` naming, and points at the `runs.get` manifest
  for Run correlation. (R7)
- One additive migration (0054); no RBAC, tool-surface, config, error-category, or destructive-gate
  change. (R8)
- Live (`live_vm`, operator-run): after a build/install/boot on a local-libvirt System with a
  post-readiness workload, `runs.get` lists the boot-evidence snapshot and the rotating parts in
  `data.console_artifacts`, and each listed `artifact_id` reads via `artifacts.get`.

## Risks

- **Coarse attribution at a Run boundary (ADR-0273 R6b residual).** A part is attributed to whichever
  Run most recently booted the System as of the (lock-held) job that seals it. The normal case — a
  new boot changes `boot_id`, resetting the generation under the per-System lock — leaves no
  old-generation straggler to misattribute. The one residual is the inherited ADR-0273 R6b
  missed-power-cycle case (`boot_id` degraded to `""` **and** a truncate-then-regrow that crossed the
  old offset before a sweep saw a shrink): there the new boot is mislabeled as the old generation and
  the old Run's ≤ `SEAM_OVERLAP` seam carry lands in a part resolved to the new Run. The console byte
  stream is already cross-contaminated at that seam by R6b; `run_id` is no worse and is bounded to the
  single straddling part (R3). Accepted, not a defect — adding per-generation sidecar state to close it
  is the rejected alternative in ADR-0279.
- **Manifest growth and truncation on a chatty long-lived System.** A multi-hour workload can
  correlate many parts to one Run. The manifest is bounded (`CONSOLE_MANIFEST_MAX`, newest-first) with
  `_total` / `_truncated` disclosure, so `runs.get` stays token-bounded. Under truncation the
  **oldest** correlated parts are dropped from the list (R5a): an early post-readiness crash signature
  in a > ~6 MiB-console Run may fall outside the returned window, and the only durable way to reach it
  is `artifacts.list(system_id)` — which is System-scoped and still mixes Runs, the residual the
  rejected run-scoped-list alternative leaves open. The boot console is always reachable via
  `refs.console`. This mirrors ADR-0273's accepted "no `artifacts.list` pagination" stance; a paged
  run-scoped manifest is named future work, not built here.
- **FK to `runs`.** `run_id REFERENCES runs (id)` means a console artifact cannot name a Run that
  does not exist. Console artifacts are reclaimed at System teardown (ADR-0273), and a Run is not
  deleted out from under a live System, so the FK does not strand rows; should a future delete path
  appear it must clear or cascade `run_id` (noted for that change, not built here).
- **NULL-attributed history.** Pre-migration console artifacts and any uncorrelated part read with
  no manifest entry. This is the explicit no-backfill non-goal: the value is durable evidence going
  forward, and a NULL is honest about "unknown Run" rather than guessing.
