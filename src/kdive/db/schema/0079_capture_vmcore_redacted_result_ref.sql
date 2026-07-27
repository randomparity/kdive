-- 0079_capture_vmcore_redacted_result_ref.sql — capture_vmcore job results become the redacted
-- vmcore artifact id (#1591, ADR-0466). Forward-only (ADR-0015), data-only: no schema change.
--
-- Historical `capture_vmcore` rows stored the RAW object key (`.../runs/{run_id}/vmcore-{method}`)
-- in `result_ref`, and `vmcore.list` was how an agent turned that into something readable. That
-- tool is gone, so `result_ref` must mean exactly one thing for every reader of `refs.result`:
-- the redacted core's artifact id, the value `artifacts.get` accepts.
--
-- Both rows are written in one transaction from the same `CaptureOutput`, so the redacted sibling's
-- key is the raw key plus `-redacted` across every provider — a deterministic join, no payload
-- parsing and no run_id lookup. FALLBACK: a row whose redacted sibling is absent (reclaimed or
-- expired artifact) resolves to NULL, which is what the correlated subquery yields on no match.
-- NULL is the honest value — `refs.result` is then simply omitted (`ToolResponse.from_job`), and
-- the agent recovers the core through `runs.get`'s `refs.vmcore` or `artifacts.list`. Leaving the
-- raw key would publish a reference no viewer can read and would give `result_ref` two meanings.
--
-- Scoped by the raw-vmcore key shape, mirroring `raw_vmcore_key`'s own predicate: an artifact id
-- contains no `/vmcore-`, so a re-run over already-migrated rows matches nothing.
UPDATE jobs j
SET result_ref = (
        SELECT a.id::text
        FROM artifacts a
        WHERE a.owner_kind = 'runs'
          AND a.object_key = j.result_ref || '-redacted'
        ORDER BY a.created_at, a.id
        LIMIT 1
    )
WHERE j.kind = 'capture_vmcore'
  AND j.result_ref LIKE '%/vmcore-%'
  AND j.result_ref NOT LIKE '%-redacted';
