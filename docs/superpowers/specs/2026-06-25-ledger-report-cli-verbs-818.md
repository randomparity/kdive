# Spec: `kdivectl` ledger report verbs (#818)

- **Issue:** [#818](https://github.com/randomparity/kdive/issues/818)
- **ADR:** [ADR-0250](../../adr/0250-ledger-report-cli-verbs.md)
- **Status:** Draft
- **Date:** 2026-06-25

## Problem

`kdivectl` exposes one accounting read verb — `ledger show --project <p>`
(`accounting.usage_project`). The two cross-project reporting tools have no CLI verb:

- `accounting.report_all_projects` — platform-wide reserved/reconciled/variance rollup,
  gated `platform_auditor`.
- `accounting.report_granted_set` — the same rollup across the caller's granted projects
  (member; optional named subset).

Both tools are `implemented` MCP tools. An operator at a terminal can pull a single
project's net usage but cannot get the auditor rollup or a granted-set summary without
driving the raw MCP transport (mint a token, call the tool directly). The CLI surface is
the only thing missing.

## Goal

Add two read-only curated verbs that mirror `ledger show`, mapping to the existing tools,
so the cross-project and granted-set rollups are reachable from `kdivectl`:

- `kdivectl ledger report-all   [--group-by principal] [--since <ts>] [--until <ts>]`
- `kdivectl ledger report-granted [--projects a,b] [--group-by principal] [--since <ts>] [--until <ts>]`

## Non-goals

- No new MCP tool, schema, RBAC role, or migration. The CLI is a FastMCP **client**
  (ADR-0089); it only adds verbs over tools that already exist.
- No CLI-side role logic. `report_all_projects` is `platform_auditor`-gated server-side; the
  CLI presents the caller's token and surfaces whatever envelope the server returns.
- No change to `ledger show` / `accounting.usage_project`.

## Behaviour

### Argument → tool-payload mapping

| flag | tool argument | shape |
|------|---------------|-------|
| `--group-by principal` | `group_by` | pass-through string; omitted when absent. Server rejects any value other than `principal` (`configuration_error`). |
| `--since <ts>` / `--until <ts>` | `window` | assembled into the `[start, end]` pair the tool takes. Omitted entirely when **both** are absent. When only one is given, the other half of the pair is `null` (a half-open window). |
| `--projects a,b` | `projects` | comma-split into a list (whitespace-trimmed, empty tokens dropped). `report-granted` only. Omitted when absent → the caller's full granted set. |

`--since`/`--until` values are passed through verbatim. The tool's window parser
(`_time_window.parse_timestamptz_window`) owns validation: it fails closed
(`configuration_error`, exit `2`) on a non-ISO-8601 or timezone-naive bound, or a
non-ordered `start >= end` range. The CLI does **not** re-validate — single source of truth,
and the server column is `timestamptz` so only the server knows the comparison zone.

### Rendering

The report tools return a **collection** envelope carrying both per-row `items` and
envelope-level `data` totals — a shape neither `_list` (drops the totals) nor `_record`
(drops the rows) surfaces. A new render path tables the rows and prints the totals as a
footer.

Row columns (from each item's `data`): `project`, `principal`, `reserved`, `reconciled`,
`variance`. Footer (from the envelope `data`): `scope`, `group_by`, `project_count`, and the
grand totals `total_reserved` / `total_reconciled` / `total_variance` (plus
`total_project` / `total_principal`, which are the rollup's `*`/`""` sentinels).

- **Table mode (default):** the rows as an aligned table, a blank line, then the footer as
  aligned `key  value` lines.
- **`--json` mode:** a single object `{"items": [...projected rows...], "totals": {...envelope data...}}`
  so a script gets both halves in one stable document.

### Exit codes

Unchanged from the curated-read contract (ADR-0089 / ADR-0098): the verb returns
`exit_code_for_envelope(envelope)`. A non-auditor token calling `report-all` gets the
server's `authorization_denied` envelope → exit `3`. A malformed window → `configuration_error`
→ exit `2`. Success → `0`.

### Help text

Each verb's sub-parser carries help text. `report-all`'s notes it requires a
`platform_auditor` token (the gate is server-side; the note saves an operator a failed call).

## Acceptance criteria

1. `kdivectl ledger report-all` calls `accounting.report_all_projects`; `report-granted`
   calls `accounting.report_granted_set` — proven through the registry-driven dispatch test.
2. `--group-by`, `--since`/`--until`, and (granted only) `--projects` map to the documented
   payload keys; omitted flags are absent from the payload; an absent window sends no
   `window`; a half-open window sends a `null` for the missing bound.
3. `--projects a,b` sends `["a", "b"]`; whitespace and empty tokens are dropped.
4. Table mode prints the row table plus a totals footer; `--json` emits
   `{"items": [...], "totals": {...}}` with the documented columns.
5. A server denial / malformed-window envelope surfaces the mapped non-zero exit code.
6. Both verbs are `read_only=True` in the registry and pass the read-only-gate test
   (their declared tool is `readOnlyHint`-annotated).
7. The `kdivectl` runbook documents both verbs, the window flags, and the auditor-role
   requirement on `report-all`.

## Edge cases

- **No `--since`/`--until`:** no `window` key → server reports all time.
- **Only `--since` (or only `--until`):** half-open pair `[ts, null]` (or `[null, ts]`).
- **Malformed / tz-naive / inverted window:** server returns `configuration_error`; the
  CLI surfaces exit `2` and the server's message, rather than silently empty output.
- **`--projects` with only empty tokens (`--projects ,`):** resolves to `[]`, which the
  server treats as an explicit empty granted set (a valid, empty rollup) — distinct from
  omitting the flag.
- **Empty rollup (`items: []`):** table prints the header and the footer; `--json` emits
  `{"items": [], "totals": {...}}`.
- **Non-auditor `report-all`:** `authorization_denied` envelope, exit `3`.
