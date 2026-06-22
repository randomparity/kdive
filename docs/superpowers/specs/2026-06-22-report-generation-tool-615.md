# Report generation tool (#615)

- **Status:** Draft
- **Date:** 2026-06-22
- **Issue:** [#615](https://github.com/randomparity/kdive/issues/615)
- **ADR:** [ADR-0212](../../adr/0212-report-generation-tool.md)

## Problem

Operators and project members need one report that summarizes the running configuration and
attached systems: hardware/system inventory (name, CPU count, RAM, disk), guest filesystem
images, active leases, stale leases, an activity report over a configurable date range, and
incurred costs. Today this data is spread across five read tools (`ops.inventory`,
`allocations.list`, `catalog.images`, `runs.list`/`jobs.list`, `accounting.report_*`), each
with its own envelope, scope, and audit. There is no consolidated report and no spreadsheet
output.

## Goals

1. A single tool call returns a complete, point-in-time report for a chosen scope and date
   window, with one authorization and one audit record.
2. Two output shapes: an **agent-friendly** structured envelope (consumed inline) and a
   **spreadsheet** (CSV and/or XLSX) downloadable by reference.
3. Reuse existing data-access and the established RBAC/audit split rather than re-deriving
   queries or inventing new authorization.
4. The section set is extensible: adding a section later is a localized change.

## Non-goals

- A durable `reports` object with its own `list`/`get` tools (reports are ephemeral renderings
  of live state; re-running is cheap and yields fresher data).
- Worker-job (async) generation. v1 is synchronous and bounded by per-section caps; the job
  variant is the recorded escalation path (ADR-0212) if caps prove too small.
- A migration or new persistent tables. The report composes existing tables; spreadsheet
  artifacts reuse the `artifacts` table with `owner_kind = 'reports'` and are reaped by a
  reconciler GC sweep (see Lifecycle / cleanup).

## Surface

A new `reports.*` plane with two tools, mirroring `accounting.report_granted_set` /
`accounting.report_all_projects`:

| Tool | Scope | RBAC |
|------|-------|------|
| `reports.generate_granted_set` | Optional named `projects` subset, else all member projects with a non-`None` role | `viewer` floor per target project (`_resolve_granted_set` shape) |
| `reports.generate_all_projects` | Every project | `PlatformRole.PLATFORM_AUDITOR`; denial audited iff caller holds ≥1 platform role |

**Parameters (both tools):**

- `window`: `[start, end]` ISO-8601 **timezone-aware** pair (either bound may be `null`), or
  omitted for all-time. Parsed by `parse_timestamptz_window`; a non-pair, tz-naive bound, or
  `start >= end` fails closed with `configuration_error`. Bounds the **activity** and **costs**
  sections only; inventory, leases, and images are point-in-time snapshots.
- `formats`: subset of `["csv", "xlsx"]`; default both. An empty list is a `configuration_error`.

`reports.generate_granted_set` additionally takes `projects: list[str] | None`.

Both are registered `read_only`, `maturity: "implemented"`, on a new `_register_report_tools`
appended to `_PLANE_REGISTRARS` in `mcp/app.py`.

## Point-in-time consistency (`as_of`)

The report is point-in-time (Goal 1). To make that real and testable, the handler captures a
single `as_of` timestamp once (`SELECT now()` on the report connection) and threads it through
`ReportScope` to every section. Sections compare against `as_of` rather than evaluating SQL
`now()` independently, so every section observes the same instant and a concurrent reconciler
expiry sweep cannot make the lease section disagree with the rest of the report. `as_of` is an
internal snapshot, not a parameter; when `window`'s end bound is omitted it defaults to `as_of`
for the time-bounded sections. Threading `as_of` as a bound parameter also makes the active/stale
lease boundary deterministic under test (inject a fixed `as_of`).

## Sections (v1)

Each section is a `ReportSection` with a stable `key`, an ordered `columns` schema, and an
async `gather(conn, scope, window, as_of) -> SectionRows`. `SectionRows` carries the row list
plus a `truncated` flag. The registry is an ordered tuple; v1:

1. **`inventory`** — systems in scope with their declared size. Columns: `system_id`,
   `name` (`domain_name`), `project`, `state`, `resource_kind`, `vcpus`, `memory_mb`, `disk_gb`.
   `resource_kind` comes from the backing `resources` row; `vcpus`/`memory_mb`/`disk_gb` come
   from the System's shape via a **LEFT JOIN** to `system_shapes` on `systems.shape =
   system_shapes.name` (the shapes table is the only uniform per-system source of all three, and
   carries `disk_gb`, which `resources.capabilities` does not). A System whose `shape` is not a
   catalog row yields nulls for those three columns — never an error. Project scope = the
   project's systems; all-projects = every system. Source:
   `systems ⋈ allocations ⋈ resources, LEFT JOIN system_shapes`.
2. **`leases`** — allocations split into **active** and **stale**, evaluated against `as_of`.
   Columns: `allocation_id`, `project`, `principal`, `state`, `lease_expiry`, `status`
   (`active` | `stale`).
   - active: `state IN ('granted','active') AND lease_expiry IS NOT NULL AND lease_expiry > %s` (`as_of`)
   - stale: `state = 'expired' OR (state IN ('granted','active') AND lease_expiry <= %s)` (`as_of`)
3. **`images`** — guest filesystem images visible to the scope. Columns: `provider`, `name`,
   `arch`, `format`, `visibility`, `owner`, `state`. Source: `image_catalog` with the same
   public-or-owned visibility predicate as `catalog.images`.
4. **`activity`** — **runs created within `window`** (v1 scope, deliberately narrow). Columns:
   `run_id`, `project`, `system_id`, `state`, `created_at`. Source: `runs` filtered by project
   scope and the half-open window (end defaults to `as_of`). v1 activity is runs only; job and
   allocation-transition activity are deferred to future registered sections (the registry makes
   that a localized addition). "Activity section complete" means: every `runs` row for the scope
   with `created_at` in `[start, end)` appears, up to the cap.
5. **`costs`** — incurred spend over `window`. Columns: `project`, `principal`, `reserved`,
   `reconciled`, `variance`. Source: `accounting.ledger.report(conn, projects=scope,
   group_by="principal", window=window)` — reuse, not new SQL. For the all-projects scope the
   project set is the `ledger ∪ budgets` universe (the `accounting.report_all_projects` shape),
   not a literal "all rows" scan.

**Bounding:** each section caps at a configured row limit (default mirrors existing list limits).
A capped `gather` returns the first N rows and `truncated=True`. Truncation surfaces in the inline
section envelope (`data.truncated`) and as a header note in the rendered sheet, so a truncated
report is never mistaken for complete.

**Extensibility:** adding a section = implement one `ReportSection` and append it to the
registry. Handlers, rendering, envelope shape, and RBAC do not change.

## Output

One `ToolResponse.collection`, `object_id = "report"`:

- **Inline (agent-friendly):** one `items[]` entry per section, each a `ToolResponse.success`
  carrying `data` = `{section, count, truncated, rows_json}` (rows serialized as a JSON-value
  list). Top-level `data` carries `scope`, `window`, `as_of`, `formats`, `section_count`, and the
  generated report UUID. **Inline byte budget:** the inline payload is bounded by a configured
  total budget (`KDIVE_REPORT_INLINE_MAX_BYTES`, analogous to `ARTIFACT_INLINE_MAX_BYTES`). A
  section whose serialized rows would exceed its share degrades to `rows_json` = a bounded preview
  (first K rows) plus `inline_truncated=true` and points the agent at the spreadsheet ref for the
  full set. The row cap bounds count; the byte budget bounds size, so a wide-column report cannot
  produce an oversized MCP response — the spreadsheet artifact, not the envelope, carries the bulk.
- **Spreadsheet (by reference):** for each requested format, artifacts written to the object
  store and surfaced in `refs` with presigned download URLs:
  - `csv`: one file per section; ref keys `csv:<section>` → presigned URL.
  - `xlsx`: one workbook, one sheet per section; ref key `xlsx` → presigned URL.

  Each artifact is written via `ObjectStore.put_artifact` with `Sensitivity.REDACTED` and a
  `report` retention class, then registered with `register_artifact_row(stored,
  owner_kind="reports", owner_id=<report uuid>)`. The presigned URL is minted in-handler via
  `presign_get(key, expires_in=ARTIFACT_DOWNLOAD_TTL_SECONDS)` wrapped in `asyncio.to_thread`
  (the artifact-reads pattern). A store outage degrades the spreadsheet refs to a
  `data["spreadsheet_unavailable"]` reason rather than failing the whole report — the inline
  report still returns.

## Lifecycle / cleanup (report artifacts are reaped, not leaked)

Report artifacts have a **synthetic** `owner_id` (no foreign key to any row), so — unlike
System-owned vmcores/transcripts, which are reaped when their System is torn down — nothing
would ever delete them. Reports are ephemeral and re-runnable, so a durable artifact + S3 object
per invocation would grow without bound. To prevent that leak, the reconciler gains a GC sweep
**`gc_report_artifacts(conn, store, retention)`** modeled on `gc_idempotency_keys`
(`reconciler/cleanup/gc.py`):

- Select `artifacts` rows with `owner_kind = 'reports'` (the `report` retention class) and
  `created_at < now() - retention`.
- Delete each object from the store (per-object failure is logged and retried next pass, not
  fatal — the `reap_orphaned_dump_volumes` pattern), then delete the row.
- `retention` is `KDIVE_REPORT_ARTIFACT_RETENTION` (default 7 days, the existing
  `DEFAULT_IDEMPOTENCY_RETENTION` value), registered as a reconciler periodic alongside the other
  GC sweeps in `reconciler/loop.py`.

A re-download after the artifact is reaped (or after the presigned URL's TTL elapses) is a cheap
re-run of the report, which also yields fresher data.

## Redaction

All free-text cell values pass through the redaction registry before being written into the
inline envelope or any rendered artifact, honoring the mandatory-redaction invariant. Numeric
and enum columns are not free text. Principal identifiers are already exposed to `viewer`s by
`accounting.report_*`, so they are reported as-is.

## Module layout

- `src/kdive/services/reports/__init__.py` — `ReportScope`, `Report`, `ReportSection` protocol,
  the section registry, and `generate_report(conn, scope, window, as_of) -> Report`.
- `src/kdive/services/reports/sections.py` — the five v1 `ReportSection` implementations
  (each `gather` composes existing data-access).
- `src/kdive/services/reports/render.py` — `render_csv(report) -> dict[str, bytes]` (per
  section) and `render_xlsx(report) -> bytes` (workbook). `openpyxl` imported here only.
- `src/kdive/mcp/tools/reports/__init__.py`, `generate.py` — tool wrappers + handlers
  (`as_of` capture, `_resolve_granted_set`, `require_platform_role`, audit, artifact write +
  presign, envelope assembly). `register(app, pool)` → `_register_report_tools`.
- `src/kdive/reconciler/cleanup/gc.py` — add `gc_report_artifacts(conn, store, retention)`;
  register it in `reconciler/loop.py` with `KDIVE_REPORT_ARTIFACT_RETENTION` (config in
  `config/core_settings.py`).

## Error handling

- Malformed `window` / empty `formats` / bad `projects` → `ToolResponse.failure_from_error`
  with `configuration_error`, before any DB read.
- All-projects denial → `authorization_denied`, with the platform-role-gated denial audit.
- Object-store outage on the spreadsheet path → degrade to inline-only with a
  `spreadsheet_unavailable` reason (best-effort, matching `artifacts.get`).

## Testing (behavior, not implementation)

- Each `ReportSection.gather`: rows present, empty scope, and the per-section cap → `truncated=True`.
- `as_of` determinism: with an injected fixed `as_of`, the active/stale boundary is exact —
  `lease_expiry == as_of` is stale (boundary is `> as_of` for active), `lease_expiry` one tick
  past `as_of` is active; window edges are half-open (start inclusive, end exclusive). The same
  `as_of` is observed by every section in one report.
- Inventory: a System whose `shape` is not in `system_shapes` yields null `vcpus`/`memory_mb`/
  `disk_gb` (LEFT JOIN), never an error; a System with a catalog shape carries all three.
- RBAC: granted-set viewer floor (member with role vs role-less vs non-member); all-projects
  `platform_auditor` allow + deny, and the denial-audit-iff-platform-role rule.
- Rendering: CSV round-trips each section's columns; XLSX has one sheet per section with the
  header row and the truncation note when capped. Driven by the domain `Report` directly.
- Envelope: category-iff-failure holds; spreadsheet refs are presigned URLs; store-outage
  degrades to inline-only without failing the tool (injected failing store seam). Inline byte
  budget: a section over its byte share degrades to a bounded preview + `inline_truncated`.
- Redaction: a planted secret in a free-text field is redacted in both inline rows and rendered
  artifacts.
- GC sweep: `gc_report_artifacts` deletes only `report`-class artifacts older than `retention`
  (object + row), leaves fresh ones and other owner kinds untouched, and a per-object store
  failure does not abort the sweep (injected store seam).
- Tests under `tests/mcp/tools/reports/`, `tests/services/reports/`, and the reconciler GC tests,
  mirroring the tree; the object store and presign are injected seams (no live S3).

## Dependencies

- `openpyxl` (pinned exact version; `uv.lock` updated; current stable looked up at
  implementation time per repo convention).
