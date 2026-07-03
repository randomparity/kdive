-- 0058_tool_invocation_args_digest.sql — args_digest + session index for the trail reader
-- (ADR-0304, #1010). Additive to 0039 (forward-only, ADR-0015). `args_digest` is a stable
-- SHA-256 hex over the call's REDACTED arguments (UsageTrackingMiddleware, same Redactor as
-- the log/telemetry boundaries): a secret-free correlation key, NOT recoverable args, so the
-- table stays operational analytics, not an audit trail. Nullable — existing rows predate it;
-- new rows are always populated (a no-arg call digests the empty mapping). The partial index
-- supports the `ops.tool_trail` primary read (a session's ordered trail); rows with no
-- agent_session (operator-cli calls) are excluded from it.
ALTER TABLE tool_invocation ADD COLUMN args_digest text;

CREATE INDEX tool_invocation_agent_session_ts_idx
    ON tool_invocation (agent_session, ts)
    WHERE agent_session IS NOT NULL;
