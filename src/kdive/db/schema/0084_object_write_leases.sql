-- 0084_object_write_leases.sql — a job's declared claim on an upload owner's object
-- prefix while it writes there (ADR-0502, #1687). Additive, forward-only (ADR-0015).
--
-- The ADR-0455 orphan sweep decides a key reclaimable from committed rows plus a store
-- HEAD and then deletes holding nothing, so a write landing after that decision and
-- before the delete is destroyed — ADR-0497 §3's disclosed residual, which its
-- finalize-side verify converts into a failed job rather than preventing. The reachable
-- writer is local-libvirt's server-side put_stream under local/runs/: it mints no upload
-- window, and precheck_run releases LockScope.RUN before the capture (ADR-0244) so a
-- multi-GiB stream is not held under it.
--
-- This is deliberately NOT upload_manifests, which ADR-0502 rejects on three counts:
-- that table is repair_abandoned_uploads' candidate set (and its _sweep_uncommitted_objects
-- deletes with no re-read, no grace and no lock — the open #1557), its PK
-- (owner_kind, owner_id) is already occupied for the same Run by
-- artifacts.create_run_upload's real window, and its manifest jsonb holds the declared
-- (name, sha256, size_bytes) set complete_build compares against, which a capture cannot
-- know before it writes.
--
-- The PK carries the holder, so two concurrent captures of one Run each hold their own
-- lease and neither releases the other's — the collision the manifest row would have had.
--
-- There is no deadline column. The lease's liveness is its holding job's: the sweep's
-- fence requires jobs.state = 'running' AND jobs.lease_expires_at > now(), which the
-- worker's heartbeat already renews for as long as the capture streams. A second,
-- independent deadline would have to exceed the longest capture, and nothing in the tree
-- bounds one — too small silently reopens this race, which is the worst failure a fence
-- can have (ADR-0502 Considered & rejected).
--
-- ON DELETE CASCADE because a lease with no jobs row protects nothing and must not
-- outlive it; reap_stale_write_leases collects the rest for table growth, since
-- capture_handler's `except Exception:` releases nothing and a killed worker releases
-- less.
CREATE TABLE object_write_leases (
    owner_kind text NOT NULL CONSTRAINT object_write_leases_owner_kind_check
                    CHECK (owner_kind IN ('runs', 'investigations')),
    owner_id   uuid NOT NULL,
    job_id     uuid NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT object_write_leases_pkey PRIMARY KEY (owner_kind, owner_id, job_id)
);
-- reap_stale_write_leases joins every row to jobs; the FK gives no index on the
-- referencing side, and a jobs row delete would otherwise sequentially scan this table.
CREATE INDEX object_write_leases_job_id_idx ON object_write_leases (job_id);
