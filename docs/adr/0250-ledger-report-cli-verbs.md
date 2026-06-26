# ADR 0250 — Expose the accounting report tools as `kdivectl` ledger verbs

- **Status:** Accepted
- **Date:** 2026-06-25
- **Issue:** [#818](https://github.com/randomparity/kdive/issues/818)
- **Spec:** [`../superpowers/specs/2026-06-25-ledger-report-cli-verbs-818.md`](../superpowers/specs/2026-06-25-ledger-report-cli-verbs-818.md)
- **Extends:** [ADR-0089](0089-operator-cli-mcp-client.md) (the curated-read-verb CLI this
  adds to), [ADR-0043](0043-platform-scoped-rbac-tier.md) (the platform RBAC tier that gates
  the report tools and splits their authorization axes).

## Context

`kdivectl` curated read verbs map one read-only MCP tool to a rendered table (ADR-0089).
The accounting surface exposes three read tools but only one verb: `ledger show` →
`accounting.usage_project`. The two cross-project reports — `accounting.report_all_projects`
(platform-wide, `platform_auditor`-gated) and `accounting.report_granted_set` (the caller's
member projects) — have no verb, so an operator must drive the raw MCP transport to read
them (#818).

Two report-specific shapes differ from the existing single-project verb and force design
choices:

1. **A half-open time window.** The tools take `window=[start, end]` of timezone-aware
   ISO-8601 strings (either may be `null`), parsed and validated server-side
   (`_time_window.parse_timestamptz_window`).
2. **A collection-plus-totals envelope.** The tools return per-row `items` *and*
   envelope-level `data` totals. The existing `_list` helper renders `items` and drops the
   totals; `_record` renders the `data` and drops the rows.

## Decision

We will add two `read_only` registry verbs under the `ledger` group —
`ledger report-all` → `accounting.report_all_projects` and `ledger report-granted` →
`accounting.report_granted_set` — with:

- **`--since` / `--until` window flags**, assembled by the verb handler into the
  `[start, end]` pair (omitted when both absent; `null` for an absent half). Values pass
  through verbatim; the server owns validation.
- **`--projects a,b`** (granted only), comma-split into the `projects` list.
- **`--group-by principal`**, a pass-through string the server validates.
- **A new `render_report` path** that tables the rows and prints the envelope totals as a
  footer; under `--json` it emits one `{"items": [...], "totals": {...}}` object.
- **Server-side authorization only.** The verbs add no role logic; `report-all`'s help text
  notes the `platform_auditor` requirement, and a non-auditor token surfaces the server's
  `authorization_denied` envelope (exit `3`).

## Consequences

- The cross-project and granted-set rollups become reachable from the operator CLI without
  the raw MCP transport, closing the gap #818 documents.
- The CLI's render layer grows a fourth shape (`render_report`) alongside list / record /
  data-list. It is additive; the existing three are unchanged.
- The window contract stays single-sourced in the server's parser — the CLI cannot drift
  from the `timestamptz` comparison semantics.
- No new tool, schema, RBAC role, or migration; the change is confined to the CLI package
  and its docs.

## Alternatives considered

- **A positional `--window <start> <end>` pair** instead of `--since`/`--until`. Rejected:
  a half-open window (only one bound) is awkward as a positional pair, and `--since`/`--until`
  read naturally and each default to "open" when omitted.
- **CLI-side window validation.** Rejected: the bound's comparison zone is a server concern
  (`ledger.ts` is `timestamptz`), and duplicating the parser invites drift. The CLI passes
  the strings through and lets the server fail closed.
- **Reusing `_list` and discarding the totals.** Rejected: the totals footer is the point of
  a rollup; dropping it would make the verb less useful than the raw tool it wraps.
- **A generic tool-call passthrough for the reports.** Rejected: ADR-0089 deliberately keeps
  the read surface a curated allowlist with a gate test; the reports get first-class verbs
  like every other curated read, not an un-curated bypass. (The fail-closed `tool call`
  passthrough already reaches them for ad-hoc use; this adds the rendered, documented verb.)
- **CLI-side `platform_auditor` pre-check on `report-all`.** Rejected: the gate is enforced
  server-side and audited there (ADR-0043 §4); a client-side check would duplicate the
  policy and could drift. The help text notes the requirement instead.
