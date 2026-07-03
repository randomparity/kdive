# ADR-0304: expose the tool_invocation trail via a read tool and add args_digest (#1010)

- Status: Accepted
- Date: 2026-07-02
- Builds on [ADR-0148](0148-rbac-scoped-tool-exposure.md) (the `tool_invocation` per-call
  analytics table and `UsageTrackingMiddleware`), [ADR-0062](0062-platform-operations.md)
  (the cross-project `platform_auditor` read shape and its `platform_audit_log`
  read-access record), [ADR-0192](0192-list-pagination-envelope.md) (the keyset cursor
  pagination envelope), and
  [ADR-0027](0027-safety-modules-secret-backend-impl.md) (the `Redactor` value redaction
  the digest is taken over). Respects
  [ADR-0270](0270-no-adr-refs-in-agent-surface.md) (no ADR refs in the agent surface).

## Context

`tool_invocation` (`0039_tool_invocation.sql`) records one best-effort row per dispatched
tool call — `principal`, `agent_session`, `project`, `tool`, `outcome ∈ {ok,error,denied}`,
`actor`, `client_id`, `ts` — written by `UsageTrackingMiddleware`. Two gaps make it
useless for post-hoc agent-failure analysis (BLACK_BOX_REVIEW.md Finding 1, generalized):

1. **No reader.** No MCP tool exposes the trail, so an agent session's ordered tool-call
   sequence cannot be reconstructed through the API.
2. **No `args_digest`.** You can see *that* a tool returned `error`, never *what* was
   attempted. The original schema comment made this explicit ("no args_digest").

The table grows with all traffic, including `jobs.wait` polling loops, so an unbounded
trail read would scan the whole table.

## Decision

### 1. `args_digest` column (additive, migration 0058)

Add a nullable `args_digest text` column to `tool_invocation`. `UsageTrackingMiddleware`
populates it on every new row with a stable SHA-256 hex digest computed over the call's
**redacted** arguments:

- The arguments mapping is redacted through the existing `Redactor` (ADR-0027), seeded
  from the app-owned `SecretRegistry` — the *same* redaction path the log/telemetry
  boundaries use. The digest is taken over the redacted structure, so it is stable for
  identical redacted args and carries no secret values. This keeps the table analytics,
  not an audit trail: the digest is a correlation key, not recoverable arguments.
- Serialization is canonical (`json.dumps(..., sort_keys=True, separators=(",", ":"),
  default=str)`) so key order and formatting do not perturb the digest. A call with no
  arguments digests the empty mapping, so the column is always present (non-null) on new
  rows. Existing rows keep `NULL` (forward-only, ADR-0015).
- The digest is computed on *every* outcome path (`ok`/`error`/`denied` and propagated
  exceptions), so a failed call records what was attempted.

A partial index `(agent_session, ts) WHERE agent_session IS NOT NULL` supports the
primary access path (a session's ordered trail); the existing `(tool, ts)` index is kept.

### 2. `ops.tool_trail` read tool

A new platform read tool, gated on `platform_auditor` (satisfied by `platform_admin`),
read-audited to `platform_audit_log` — the identical posture to `audit.query`'s
cross-project form (ADR-0062 §6). The trail is cross-tenant per-call data, so it takes
the same forensic role as the cross-project audit read, not the wider `platform_operator`
grant; a denial is audited only when the caller holds ≥1 platform role (ADR-0043 §4).

Filters: `agent_session`, `principal`, `tool`, and a `[start, end]` timestamptz `window`.
Keyset-paginated newest-first on `(ts, id)` via the ADR-0192 cursor helpers, mirroring
`audit.query`. Each item carries `tool`, `outcome`, `args_digest`, `ts`, `principal`,
`agent_session`, `project`, `actor`, `client_id`.

**Bounded by default.** When the caller supplies no window start, the read defaults the
lower bound to `now - 24h`, so a default call never scans the whole table. An explicit
start (even older) is honored — the default only fills an absent bound.

### 3. Docs

The tool appears in the generated tool reference, the RBAC role→tool matrix, and the
packaged doc-resource snapshots (all regenerated). No new toolset guide (the `ops.*`
platform tools have none). Existing `tool_invocation` analytics behavior is unchanged.

## Consequences

Given an `agent_session`, a platform auditor retrieves the ordered `(tool, outcome,
args_digest, ts)` trail through the API (the #1010 acceptance). `args_digest` is stable
for identical redacted args and present on new rows. The write path gains one digest
computation and one INSERT column per call; redaction reuses the cached, version-tracked
`Redactor` so no per-call registry rebuild occurs.

## Rejected alternatives

- **Raw args (not a digest).** Would turn the analytics table into an audit trail
  carrying secrets; the issue explicitly calls for a digest.
- **A second redaction path for the digest.** Reuses the existing `Redactor` so the
  digest can never diverge from the log/telemetry redaction contract.
- **`platform_operator` gate.** Exposing cross-tenant per-call data at operator would be
  a wider grant than the existing cross-tenant audit read; auditor is least-privilege and
  consistent with `audit.query`/`inventory.list`/`accounting.report_all_projects`.
- **Unbounded default read.** The table grows with all traffic (polling included); a
  default lower bound prevents a full-table scan on the common call.
- **Ascending (oldest-first) order.** Newest-first reuses the proven ADR-0192 keyset path
  unchanged; draining pages still reconstructs the full ordered trail.
