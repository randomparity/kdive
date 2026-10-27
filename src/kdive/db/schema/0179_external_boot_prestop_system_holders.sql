-- 0179_external_boot_prestop_system_holders.sql — ADR-0583 pre-stop System-holder recheck (#3353).
--
-- Immediately before the first stop of a PREPARE, the destination-side lane asks core, through
-- the same authority-bound snapshot that admits the mutation, whether a debug session or another
-- System-holding job exists. It is a separate function rather than a clause of the preparation
-- resolver because that resolver is also the post-mutation recheck: a session attaching during
-- the stop must not turn a completed power change into `superseded`.

CREATE FUNCTION public.external_boot_prestop_system_holders(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint,
    p_ack_sequence bigint, p_ack_digest text
) RETURNS text
LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT CASE
        WHEN EXISTS (
            SELECT 1
            FROM public.debug_sessions AS s
            JOIN public.runs AS r ON r.id = s.run_id
            WHERE r.system_id = bound.system_id AND s.state IN ('attach', 'live')
        ) THEN 'session_active'
        WHEN EXISTS (
            SELECT 1
            FROM public.jobs AS j
            WHERE j.state IN ('queued', 'running') AND j.id <> bound.job_id
              AND j.payload->>'system_id' = bound.system_id::text
        ) OR EXISTS (
            SELECT 1
            FROM public.jobs AS j
            WHERE j.state IN ('queued', 'running') AND j.id <> bound.job_id
              AND j.payload->>'run_id' IN (
                  SELECT r.id::text FROM public.runs AS r WHERE r.system_id = bound.system_id
              )
        ) THEN 'job_active'
        ELSE 'clear'
    END
    FROM public.resolve_current_external_boot_preparation_authority(
        p_peer_incarnation, p_authority_id, p_generation, p_ack_sequence, p_ack_digest, 'prepare'
    ) AS resolved
    CROSS JOIN LATERAL (
        SELECT a.system_id, a.job_id
        FROM public.external_boot_authorities AS a
        WHERE a.id = resolved.authority_id
    ) AS bound
$$;

REVOKE ALL ON FUNCTION public.external_boot_prestop_system_holders(
    text, uuid, bigint, bigint, text
) FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
    kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.external_boot_prestop_system_holders(
    text, uuid, bigint, bigint, text
) TO kdive_provider_authority;
