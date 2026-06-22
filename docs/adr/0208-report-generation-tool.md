# ADR 0208 — Report generation plane: composed cross-cutting report with spreadsheet artifacts

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-22
- **Deciders:** Platform / core-platform

## Context

Issue #615 asks for a tool that generates a report on the running configuration and
attached systems: an inventory of hardware/systems (name, CPU count, RAM, disk), guest
filesystem images, active leases, stale leases, an activity report with configurable date
options, and incurred costs — with an agent-friendly output and a spreadsheet output.

Most of the underlying data is already reachable through individual read tools, each with
its own envelope, RBAC, and audit: `ops.inventory` and the `systems`/`allocations`/`resources`
join (inventory), `allocations.list` and `lease_expiry` (leases), `catalog.images`
(`image_catalog`), `runs.list`/`jobs.list` (activity), and `accounting.report_*` over the
`ledger`/`budgets` tables (costs). The new requirement is a single **consolidated** read that
joins these into one report for a chosen scope and time window, plus a downloadable
**spreadsheet** rendering — a shape no existing tool provides.

Two seams constrain the design. First, the artifact-retrieval tool `artifacts.get`
(`mcp/tools/catalog/artifacts/reads.py`) is hard-wired to `owner_kind = 'systems'` and
resolves the caller's read authorization by joining `owner_id → systems.project`; an artifact
that is not owned by a System is not retrievable through it. Second, the cross-cutting
invariants in `CLAUDE.md` apply: the uniform `ToolResponse` envelope ("references, never log
dumps"), mandatory redaction before any persistence or response snippet, and the established
project-scoped-vs-platform-auditor RBAC split that `accounting.report_granted_set` /
`accounting.report_all_projects` already model.

## Decision

Add a new `reports.*` tool plane with two tools that mirror the accounting reporting split:

- **`reports.generate_granted_set`** — reports on the caller's granted project(s) (optional
  named `projects` subset, else all member projects with a non-`None` role), `viewer` floor
  per target project, using the same `_resolve_granted_set` authorization shape as accounting.
- **`reports.generate_all_projects`** — reports across every project, gated by
  `PlatformRole.PLATFORM_AUDITOR`, with the same "audit the denial only if the caller holds
  ≥1 platform role" semantics as `accounting.report_all_projects`.

Both accept a `window` (`[start, end]` ISO-8601 timestamptz pair, half-open, via the existing
`parse_timestamptz_window` helper) that bounds the time-sensitive sections, and a `formats`
list selecting the spreadsheet renderings (`csv`, `xlsx`, or both — both is the default).

A report is composed of **sections** behind a small registry seam (a `ReportSection`
protocol: a stable key, a `gather(conn, scope, window) -> rows` coroutine, and a column
schema). v1 registers five sections — inventory, leases (active + stale), images, activity,
costs — and adding a section later is a localized change (register one more `ReportSection`);
the tool handlers, rendering, and envelope shape do not change. Each section's `gather`
composes the existing data-access functions rather than re-deriving SQL where a domain helper
exists (e.g. costs reuses `accounting.ledger.report()`).

Generation is **synchronous** in the server tool handler — the same process and read-aggregation
model as `accounting.report_all_projects`, which already scans `ledger`/`budgets` synchronously.
Each section is bounded by a documented per-section row cap; a capped section sets a
`truncated` flag in its inline envelope and in the rendered sheet header so a truncated report
is never mistaken for a complete one.

The report is returned in **two shapes from one envelope**:

- **Agent-friendly:** the full report inline as `ToolResponse` `data`/`items` — one item per
  section carrying its rows and `truncated` flag.
- **Spreadsheet:** for each requested format, one artifact written to the object store — CSV
  emits one file per section (a small set of keyed `refs`), XLSX emits one workbook with one
  sheet per section. Each artifact is written with `Sensitivity.REDACTED` and registered as an
  `artifacts` row with `owner_kind = 'reports'`, a freshly generated report UUID as `owner_id`,
  and a `report` retention class. Because `artifacts.get` is System-scoped, `reports.generate`
  **mints the presigned download URL itself** (the same `presign_get` + `asyncio.to_thread`
  path the artifact reads use) and returns it in `refs`; re-fetching a stale report is a cheap
  re-run, which also yields fresher data.

All free-text fields pass through the redaction registry before being written into the inline
envelope or the rendered artifacts. `owner_kind = 'reports'` needs no migration: `artifacts.owner_kind`
is a free-text column with no `CHECK` constraint, and the report composes existing tables only.
XLSX rendering adds one pinned dependency, `openpyxl`.

## Consequences

- **Easier:** one call returns a complete, consistent point-in-time report for a scope, both as
  structured data an agent consumes directly and as spreadsheets a human downloads. The five
  data domains are joined once with one authorization and one audit record instead of the agent
  fanning out across five tools and reconciling five envelopes.
- **Harder / new obligations:**
  - A new tool plane and `services/reports/` package (gather + render). The rendering layer is a
    new surface that must round-trip every section's column schema for both CSV and XLSX.
  - `openpyxl` is a new runtime dependency (pinned, `uv.lock` updated) and new supply-chain
    surface; it is only imported on the XLSX render path.
  - Report artifacts are owned by `owner_kind = 'reports'` with a synthetic `owner_id` that is
    not a foreign key to any table, so their lifecycle is governed only by the `report` retention
    class, not by an owning row's deletion. The presigned URL is the sole retrieval path and is
    time-boxed; there is no durable `reports.get`.
  - Synchronous generation bounds report size by per-section caps. A report that needs the full
    unbounded fleet/ledger is explicitly out of scope for v1 and would motivate the worker-job
    variant below.
- **Unchanged:** the existing per-domain read tools, `artifacts.get`'s System scope, the
  `ToolResponse` envelope, and the accounting RBAC/audit helpers, which the new plane reuses.

## Alternatives considered

- **Generate the spreadsheet in a worker job** (return `{job_id, status: running}`, render and
  upload in the worker, retrieve via polling). This is the right model for an unbounded report,
  but it adds a `JobKind`, a handler, and a poll cycle for what is a bounded synchronous read in
  v1; `accounting.report_all_projects` sets the precedent that a cross-tenant aggregation read is
  acceptable synchronously. Recorded as the settled escalation path if per-section caps prove
  too small in practice.
- **Extend `accounting.*` with a fuller report** instead of a new plane. Rejected: the report is
  not an accounting concern — it joins inventory, leases, images, and activity — and overloading
  the accounting namespace would mix cost-only rollups with a broader operational report.
- **Reuse `artifacts.get` for retrieval** by generalizing it to a `reports` owner kind. Rejected
  for v1: it widens a security-sensitive, System-scoped read path (its project-authorization join
  assumes a System owner) for a marginal benefit, since a point-in-time report is cheaply
  re-runnable and a fresh run is preferable to a stale cached one.
- **CSV-only** (no new dependency). Rejected per the issue's explicit spreadsheet ask and the
  operator preference for a true multi-sheet workbook; both formats are offered.
- **Persist a durable `reports` row + table** so a report is a first-class object with its own
  list/get tools. Rejected as scope the issue does not ask for; reports are ephemeral renderings
  of live state, and persisting them invites staleness and a retention story without a consumer.
