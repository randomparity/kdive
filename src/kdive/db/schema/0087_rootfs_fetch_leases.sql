-- 0087_rootfs_fetch_leases.sql — a fetcher's declared claim on an investigation's
-- uploaded-rootfs base while it stages one (ADR-0515, #1702/#1558 option 2). Additive,
-- forward-only (ADR-0015).
--
-- The ADR-0441 §6 pin classifier answers "does a System pin this base?" from the System's
-- state column plus overlay presence, and both terminal states a doomed provision reaches
-- defeat it: _ROOTFS_REFERENCERS_SQL excludes torn_down outright, FAILED is outside
-- ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES, and a System that died mid-fetch never got an
-- overlay. ADR-0495's flock probe covers the transfer itself but not the window before
-- it: a fetch that has resolved its artifacts row has not yet created a partial, and it
-- waits on the per-(investigation, checksum) session lock in between, so that window is
-- not short. This table is the durable evidence that window needs.
--
-- The row carries the holder, so two sibling Systems staging the same base each hold
-- their own lease and neither releases the other's — the collision a
-- (investigation_id, token) primary key would have had, and the same reason
-- object_write_leases (ADR-0502) puts job_id in its PK.
--
-- expires_at is NOT the second, independent deadline ADR-0502 rejected for its own
-- lease. That lease could fence on its holding job's heartbeat-renewed
-- jobs.lease_expires_at; this one cannot, because the provision seam passes the fetch a
-- RootfsUploadContext with no job identity and the UploadFetch callable has no job_id to
-- fence on. Absent a heartbeat, a fetcher killed by SIGKILL releases nothing — failed and
-- torn_down are terminal, so nothing else ever clears its row — and an unbounded pin is
-- the disk-exhaustion regression AC-8 exists to catch. The deadline is therefore
-- load-bearing here rather than redundant, and it is set from Postgres now() at acquire
-- so no worker's clock enters the comparison. ADR-0515 derives the interval and states
-- the residual leak window it buys.
--
-- ON DELETE CASCADE on both parents because a lease whose investigation or System is gone
-- pins nothing that can still be provisioned, and must not outlive either.
CREATE TABLE rootfs_fetch_leases (
    id               uuid PRIMARY KEY,
    investigation_id uuid NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    -- The base64url content-address token, the same derivation staged_rootfs_path writes
    -- and _rootfs_token_from_key reads back off the object key. Not the raw checksum: the
    -- reclaim gate holds a token and comparing it here beats transcoding per probe.
    token            text NOT NULL,
    system_id        uuid NOT NULL REFERENCES systems (id) ON DELETE CASCADE,
    acquired_at      timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz NOT NULL
);

-- The pin probe's whole predicate: "is any unexpired lease held for this
-- (investigation, token)?", run under the INVESTIGATION advisory lock once per due
-- checksum. Leading with the two equality columns and carrying expires_at makes it an
-- index-only range scan rather than a heap lookup per candidate row.
CREATE INDEX rootfs_fetch_leases_pin_idx
    ON rootfs_fetch_leases (investigation_id, token, expires_at);

-- The reaper's worklist, and the reason it is not a sequential scan of a table whose live
-- set is normally near-empty but whose dead set grows with every killed fetcher.
CREATE INDEX rootfs_fetch_leases_expires_at_idx ON rootfs_fetch_leases (expires_at);

-- Both FKs give no index on the referencing side, so a systems row delete — which the
-- reconciler does drive — would otherwise sequentially scan this table. investigation_id
-- is already covered by rootfs_fetch_leases_pin_idx's leading column.
CREATE INDEX rootfs_fetch_leases_system_id_idx ON rootfs_fetch_leases (system_id);
