-- 0114_host_dump_volume_leases.sql — a capture job's declared claim on its System's
-- deterministic host_dump volume while it operates on it (ADR-0562, #1955). Additive,
-- forward-only (ADR-0015).
--
-- The ADR-0094 orphaned-volume sweep asked whether the System had a running capture job and
-- then deleted, holding nothing. ADR-0557 made that predicate effective and disclosed what it
-- cannot be: a queued job is deliberately not a live holder, so a worker that claims the job
-- between the sweep's classification and its delete makes the answer stale. The capture then
-- deletes the prior orphan and creates a NEW volume at the same deterministic name, and the
-- sweep's name-addressed delete resolves to that one.
--
-- A row here is the declaration the sweep can see. It is minted under
-- pg_advisory_xact_lock(SYSTEM, system_id) — the lock the sweep now holds across both its final
-- classification and its delete — so a mint either precedes the classification or blocks until
-- the delete is done. That ordering is the closure; a third state predicate would only narrow
-- the window.
--
-- This is deliberately NOT object_write_leases (ADR-0502). That table's owner_kind is an
-- *upload* owner kind, its rows mean "objects are being written under this store prefix", and
-- lock_scope_for maps them to the scopes the ADR-0455 orphan sweep locks. A libvirt volume is
-- not a store prefix, and the sweep that reads that table would begin skipping object keys for
-- a System that has none.
--
-- The PK carries the holder, so a retried attempt is a no-op rather than a conflict and no
-- holder can release another's row.
--
-- There is no deadline column. The lease's liveness is its holding job's:
-- jobs.state = 'running' AND jobs.lease_expires_at > now(), which the worker's heartbeat renews
-- for as long as the capture runs (kdive.artifacts.write_lease.LIVE_HOLDER_SQL, shared verbatim
-- with the sweep that honours it). A second, independent deadline would have to exceed the
-- longest capture and nothing in the tree bounds one — too small silently reopens this race,
-- which is the worst failure a fence can have (ADR-0502 Considered & rejected).
--
-- ON DELETE CASCADE on both identities because a lease naming a gone job or a gone System
-- protects nothing; reap_stale_host_dump_volume_leases collects the far commoner case of a job
-- that merely stopped running, since capture_handler's `except Exception:` releases nothing and
-- a killed worker releases less.
CREATE TABLE public.host_dump_volume_leases (
    system_id  uuid NOT NULL REFERENCES systems (id) ON DELETE CASCADE,
    job_id     uuid NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT host_dump_volume_leases_pkey PRIMARY KEY (system_id, job_id)
);

-- The stale-lease reap joins every row to jobs, and the FK gives no index on the referencing
-- side, so a jobs row delete would otherwise sequentially scan this table. system_id is the
-- PK's leading column and needs none of its own.
CREATE INDEX host_dump_volume_leases_job_id_idx ON public.host_dump_volume_leases (job_id);

-- The worker mints and (on the success path) releases; the reconciler reads the fence and
-- collects rows whose holder is no longer live. Neither writes anything else, and neither the
-- server nor the lifecycle witness mutates the table at all — nothing on the request path
-- creates or inspects a dump volume. The server's read-only grant keeps the shape every other
-- ordinary table has (0107): read for support and diagnostics, no mutation.
GRANT SELECT, INSERT, DELETE ON TABLE public.host_dump_volume_leases TO kdive_worker;
GRANT SELECT, DELETE ON TABLE public.host_dump_volume_leases TO kdive_reconciler;
GRANT SELECT ON TABLE public.host_dump_volume_leases TO kdive_server;
