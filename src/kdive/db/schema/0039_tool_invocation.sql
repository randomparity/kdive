-- 0039_tool_invocation.sql — per-call usage analytics (ADR-0148, #506).
-- Append-only, modelled on platform_audit_log: no project-membership guard (a list-time or
-- object-resolving call may carry no resolvable project, so `project` is nullable). This is
-- operational analytics, NOT an audit trail (no args_digest) and distinct from audit_log /
-- platform_audit_log. `outcome` is CHECK-constrained to the closed set so a bad
-- classification fails loud. `actor` reuses the operator-cli|agent|unknown classification
-- (ADR-0089), NOT NULL with a default so the column is total. Recorded best-effort by
-- UsageTrackingMiddleware. Retention/aggregation is a future concern (the table grows with
-- traffic, polling loops included); a `(tool, ts)` index supports the eventual trend reads.
CREATE TABLE tool_invocation (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ts            timestamptz NOT NULL DEFAULT now(),
    principal     text NOT NULL,
    agent_session text,
    project       text,
    tool          text NOT NULL,
    outcome       text NOT NULL CHECK (outcome IN ('ok', 'error', 'denied')),
    actor         text NOT NULL DEFAULT 'agent',
    client_id     text
);

CREATE INDEX tool_invocation_tool_ts_idx ON tool_invocation (tool, ts);
