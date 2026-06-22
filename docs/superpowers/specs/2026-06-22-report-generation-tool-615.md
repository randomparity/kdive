# Report generation tool (#615)

- **Status:** Draft
- **Date:** 2026-06-22
- **Issue:** [#615](https://github.com/randomparity/kdive/issues/615)
- **ADR:** [ADR-0208](../../adr/0208-report-generation-tool.md)

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
  variant is the recorded escalation path (ADR-0208) if caps prove too small.
- A migration or new persistent tables. The report composes existing tables; spreadsheet
  artifacts reuse the `artifacts` table with `owner_kind = 'reports'`.

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

## Sections (v1)

Each section is a `ReportSection` with a stable `key`, an ordered `columns` schema, and an
async `gather(conn, scope, window) -> list[Row]`. The registry is an ordered tuple; v1:

1. **`inventory`** — systems in scope with backing hardware specs. Columns: `system_id`,
   `name` (`domain_name`), `project`, `state`, `resource_kind`, `vcpus`, `memory_mb`, `disk_gb`.
   vCPU/RAM/disk come from `resources.capabilities` (via the `capability_view` / structured
   `ResourceCapabilities`). Project scope = the project's systems joined to their backing
   resources; all-projects = every system. Source: `systems ⋈ allocations ⋈ resources`.
2. **`leases`** — allocations split into **active** and **stale**. Columns: `allocation_id`,
   `project`, `principal`, `state`, `lease_expiry`, `status` (`active` | `stale`).
   - active: `state IN ('granted','active') AND lease_expiry IS NOT NULL AND lease_expiry > now()`
   - stale: `state = 'expired' OR (state IN ('granted','active') AND lease_expiry <= now())`
3. **`images`** — guest filesystem images visible to the scope. Columns: `provider`, `name`,
   `arch`, `format`, `visibility`, `owner`, `state`. Source: `image_catalog` with the same
   public-or-owned visibility predicate as `catalog.images`.
4. **`activity`** — runs created within `window`. Columns: `run_id`, `project`, `system_id`,
   `state`, `created_at`. Source: `runs` filtered by project scope and the half-open window.
5. **`costs`** — incurred spend over `window`. Columns: `project`, `principal`, `reserved`,
   `reconciled`, `variance`. Source: `accounting.ledger.report(conn, projects=scope, group_by="principal", window=window)` — reuse, not new SQL.

**Bounding:** each section caps at a configured row limit (default mirrors existing list limits).
A capped `gather` returns the first N rows and a `truncated=True` marker. Truncation surfaces in
the inline section envelope (`data.truncated`) and as a header note in the rendered sheet.

**Extensibility:** adding a section = implement one `ReportSection` and append it to the
registry. Handlers, rendering, envelope shape, and RBAC do not change.

## Output

One `ToolResponse.collection`, `object_id = "report"`:

- **Inline (agent-friendly):** one `items[]` entry per section, each a `ToolResponse.success`
  carrying `data` = `{section, count, truncated, rows_json}` (rows serialized as a JSON-value
  list). Top-level `data` carries `scope`, `window`, `formats`, `section_count`, and the
  generated report UUID.
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

## Redaction

All free-text cell values pass through the redaction registry before being written into the
inline envelope or any rendered artifact, honoring the mandatory-redaction invariant. Numeric
and enum columns are not free text. Principal identifiers are already exposed to `viewer`s by
`accounting.report_*`, so they are reported as-is.

## Module layout

- `src/kdive/services/reports/__init__.py` — `ReportScope`, `Report`, `ReportSection` protocol,
  the section registry, and `generate_report(conn, scope, window) -> Report`.
- `src/kdive/services/reports/sections.py` — the five v1 `ReportSection` implementations
  (each `gather` composes existing data-access).
- `src/kdive/services/reports/render.py` — `render_csv(report) -> dict[str, bytes]` (per
  section) and `render_xlsx(report) -> bytes` (workbook). `openpyxl` imported here only.
- `src/kdive/mcp/tools/reports/__init__.py`, `generate.py` — tool wrappers + handlers
  (`_resolve_granted_set`, `require_platform_role`, audit, artifact write + presign, envelope
  assembly). `register(app, pool)` → `_register_report_tools`.

## Error handling

- Malformed `window` / empty `formats` / bad `projects` → `ToolResponse.failure_from_error`
  with `configuration_error`, before any DB read.
- All-projects denial → `authorization_denied`, with the platform-role-gated denial audit.
- Object-store outage on the spreadsheet path → degrade to inline-only with a
  `spreadsheet_unavailable` reason (best-effort, matching `artifacts.get`).

## Testing (behavior, not implementation)

- Each `ReportSection.gather`: rows present, empty scope, the active/stale lease boundary
  (`lease_expiry` exactly at / just past `now()`), window edges (half-open: start inclusive,
  end exclusive), and the per-section cap → `truncated=True`.
- RBAC: granted-set viewer floor (member with role vs role-less vs non-member); all-projects
  `platform_auditor` allow + deny, and the denial-audit-iff-platform-role rule.
- Rendering: CSV round-trips each section's columns; XLSX has one sheet per section with the
  header row and the truncation note when capped. Driven by the domain `Report` directly.
- Envelope: category-iff-failure holds; spreadsheet refs are presigned URLs; store-outage
  degrades to inline-only without failing the tool (injected failing store seam).
- Redaction: a planted secret in a free-text field is redacted in both inline rows and rendered
  artifacts.
- Tests under `tests/mcp/tools/reports/` and `tests/services/reports/`, mirroring the tree;
  the object store and presign are injected seams (no live S3).

## Dependencies

- `openpyxl` (pinned exact version; `uv.lock` updated; current stable looked up at
  implementation time per repo convention).
