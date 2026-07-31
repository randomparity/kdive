-- 0090_rootfs_fetch_lease_job_fence.sql — the rootfs fetch lease is fenced on its
-- holding job, not on a derived deadline (ADR-0522, #1740). Forward-only (ADR-0015);
-- 0087 is immutable and is never edited.
--
-- The jump from 0087 is deliberate and is not a lost file. This work was authored as
-- 0088 while two sibling branches held 0088/0089; ADR-0517's ordering guard requires a
-- version strictly above the base branch maximum, not exactly one above it, precisely so
-- an abandoned number leaves a legitimate gap rather than forcing a renumber race. If
-- neither sibling lands, 0088 and 0089 simply stay unused.
--
-- 0087 lines 19-28 stated the precondition this migration discharges: that lease could
-- not fence on jobs.lease_expires_at "because the provision seam passes the fetch a
-- RootfsUploadContext with no job identity and the UploadFetch callable has no job_id to
-- fence on". #1740 threads the provision job's id through that seam, so the handle now
-- exists and expires_at stops being load-bearing.
--
-- What the swap buys, in one sentence: a fetcher killed by SIGKILL used to pin its base
-- for up to 6 hours (0087's derived TTL), and now stops pinning it as soon as its job
-- lease lapses on the worker's heartbeat interval. The derived constant, the 50 GiB cap
-- and the 5 MiB/s floor rate it came from all go away with it.
--
-- job_id, not job_id NULL-able: a lease with no holder is exactly the unbounded pin
-- 0087's expires_at existed to prevent, so the column that replaces the deadline must
-- not be optional. A fetch that cannot name its job records no lease at all and says so
-- (acquire_fetch_lease's degrade path) — an unleased fetch reverts the reclaim to its
-- pre-ADR-0515 reach, which is a rare and survivable race, where a holderless row would
-- be a permanent leak that reads as protection in the table an operator inspects.
--
-- ON DELETE CASCADE matches object_write_leases (ADR-0502 / migration 0084): a lease with
-- no jobs row protects nothing and must not outlive it. It is the same relationship, so
-- it gets the same rule.
--
-- The DELETE is not data loss. Every pre-existing row is either dead (its fetcher was
-- killed, and 0087's own reap would have collected it) or held by a fetcher this upgrade
-- has no way to name a job for; neither can satisfy the new fence, so keeping them would
-- mean a NOT NULL column with nothing to put in it. The rows are transient evidence about
-- in-flight downloads, not records of anything — the worst case is one reclaim pass that
-- may delete a base a fetch started before the upgrade is still staging, which is
-- precisely the pre-ADR-0515 behaviour and is bounded by that single pass, because
-- ADR-0495's flock probe still covers any such fetch that has reached its partial.
DELETE FROM rootfs_fetch_leases;

ALTER TABLE rootfs_fetch_leases
    ADD COLUMN job_id uuid NOT NULL REFERENCES jobs (id) ON DELETE CASCADE;

-- The pin probe's whole predicate is now "is any lease for this (investigation, token)
-- held by a live job?", so the deadline leaves the index with it. Carrying job_id keeps
-- the scan index-only up to the jobs lookup the liveness test then makes.
DROP INDEX rootfs_fetch_leases_pin_idx;
CREATE INDEX rootfs_fetch_leases_pin_idx
    ON rootfs_fetch_leases (investigation_id, token, job_id);

-- The reaper's worklist is no longer a deadline range: reap_dead_fetch_leases selects on
-- the jobs join, which rootfs_fetch_leases_job_id_idx below serves.
DROP INDEX rootfs_fetch_leases_expires_at_idx;

ALTER TABLE rootfs_fetch_leases DROP COLUMN expires_at;

-- Both the reap and the FK need this. The FK gives no index on the referencing side, so a
-- jobs row delete — which the job reaper does drive — would otherwise sequentially scan
-- this table; object_write_leases_job_id_idx exists for the same two reasons.
CREATE INDEX rootfs_fetch_leases_job_id_idx ON rootfs_fetch_leases (job_id);
