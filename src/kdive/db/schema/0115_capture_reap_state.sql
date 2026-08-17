-- 0115_capture_reap_state.sql — reap-once convergence for the capture-reclamation sweep
-- (ADR-0556, #1946). Additive, forward-only (ADR-0015).
--
-- ADR-0555 bounded repeated work with a lookback window. ADR-0556 replaced it because a lookback
-- is a weaker convergence mechanism, not a substitute for persisted completion: it permanently
-- abandons every orphan older than the window — including the entire backlog present at deploy —
-- while still revisiting every candidate inside it on every reconciler pass. A row here is the
-- persisted completion the sweep writes instead, so a resolved capture leaves the candidate set
-- after one successful attempt and an idle deployment does zero work.
--
-- Deliberately a side table rather than columns on jobs. The state is capture-only and the sweep
-- is its only writer, so widening the platform's busiest table with four columns 99% of its rows
-- would never use buys nothing; a job with no row here has simply never been swept, which is
-- exactly the "untouched" case selection has to distinguish anyway.
--
-- A row is in exactly one of two conditions, and the shape check makes any third condition
-- unrepresentable:
--
--   reclaimed — reclaimed_at set, retry_after NULL. Terminal. The provider reported that its
--               call left no capture state behind, so no later pass considers the job.
--   deferred  — retry_after set, attempts > 0. The attempt that wrote it did not reclaim, so the
--               row is eligible again once the database clock passes the deadline.
--
-- retry_after is a database-clock deadline, never a Python one: the reconciler's clock is not the
-- reference clock for anything else in this sweep, and a skewed host would otherwise either
-- monopolise a degraded provider or postpone recovery indefinitely. The sweep advances it beyond
-- both its prior value and the current database time with bounded backoff, so a persistently
-- failing row cannot come back every pass.
--
-- Ordering note for the sweep, which this shape exists to serve: untouched rows have no row here
-- at all, so selection must sort on an explicit `(state row exists)` discriminator rather than on
-- a NULL retry_after. Untouched rows therefore sort ahead of a just-failed row even when its
-- backoff has already expired, and persistent old failures cannot starve later candidates.
--
-- ON DELETE CASCADE because reap state naming a job that no longer exists records nothing; the
-- row-keyed design already accepts that a capture whose job row is absent is undiscoverable.
CREATE TABLE public.capture_reap_state (
    job_id       uuid PRIMARY KEY REFERENCES public.jobs (id) ON DELETE CASCADE,
    attempts     integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    retry_after  timestamptz,
    reclaimed_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT capture_reap_state_shape CHECK (
        (reclaimed_at IS NOT NULL AND retry_after IS NULL AND attempts > 0)
        OR (reclaimed_at IS NULL AND retry_after IS NOT NULL AND attempts > 0)
    )
);

CREATE TRIGGER capture_reap_state_set_updated_at BEFORE UPDATE ON public.capture_reap_state
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

-- Selection anti-joins every eligible capture job against this table and, for the rows it finds,
-- orders by the retry deadline. A partial index over the deferred rows serves both: reclaimed
-- rows are terminal and never ordered, and there are eventually far more of them.
CREATE INDEX capture_reap_state_retry_after_idx
    ON public.capture_reap_state (retry_after)
    WHERE reclaimed_at IS NULL;

-- The reconciler is the sweep, and the sweep is the only writer: it inserts a row on the first
-- outcome for a job and updates it on every later one. It never deletes — the job's own deletion
-- cascades — so no DELETE grant. The server's read-only grant keeps the shape every other
-- ordinary table has (0107): read for support and diagnostics, no mutation. The worker does not
-- appear at all; #1949 owns whether a capture attempt clears prior completion, and granting for
-- that now would be authority nothing exercises.
GRANT SELECT, INSERT, UPDATE ON TABLE public.capture_reap_state TO kdive_reconciler;
GRANT SELECT ON TABLE public.capture_reap_state TO kdive_server;

-- The sweep's pre-cutover evidence path reads the durable cutover generation: a job with no
-- supervised attempt is covered only when the generation is complete and the job's created_at is
-- no later than the committed cutoff. A column grant rather than a table grant, matching how 0113
-- exposed capture_operations to the reconciler — the cutoff table stays guarded evidence that no
-- role holds table-level access to, and operation_quiescent stays out of reach because the sweep
-- must consult the aggregate `complete` flag, never one of its two halves.
GRANT SELECT (singleton, complete, cutoff_at)
    ON TABLE public.capture_operation_cutoff TO kdive_reconciler;
