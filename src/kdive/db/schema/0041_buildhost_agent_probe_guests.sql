-- 0041_buildhost_agent_probe_guests.sql — reaper-visible markers for doctor ephemeral
-- build-host guest-agent probe builders (ADR-0167, #544/#531). Additive (forward-only, ADR-0015).
--
-- The `ephemeral_libvirt_buildhost_agent` doctor check provisions a throwaway `kdive-build-<run_id>`
-- builder per ephemeral_libvirt host (ADR-0100) and execs a trivial command over its guest agent.
-- The builder is a real build-VM domain the reconciler's `reap_orphan_build_vms` sweep already owns
-- (it reaps a build VM whose owning BUILD job is gone) — but a doctor probe has no BUILD job, so
-- without a marker the sweep would reap the probe mid-check. Each probe registers a row here under
-- the builder's `run_id`, carrying an active-run heartbeat (`heartbeat_at`) and a hard TTL
-- (`ttl_deadline`). `reap_orphan_build_vms` treats a `kdive-build-<run_id>` domain whose run_id has a
-- fresh, unreleased probe heartbeat as live; a stale one is reaped, with `ttl_deadline` as backstop.
CREATE TABLE buildhost_agent_probe_guests (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    build_host_id uuid        NOT NULL REFERENCES build_hosts (id) ON DELETE CASCADE,
    run_id        uuid        NOT NULL UNIQUE,
    heartbeat_at  timestamptz NOT NULL DEFAULT now(),
    ttl_deadline  timestamptz NOT NULL,
    released_at   timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- At most one live (not-yet-released) probe per build host: the DB-level single-flight fence.
CREATE UNIQUE INDEX buildhost_agent_probe_guests_one_live_per_host
    ON buildhost_agent_probe_guests (build_host_id)
    WHERE released_at IS NULL;
