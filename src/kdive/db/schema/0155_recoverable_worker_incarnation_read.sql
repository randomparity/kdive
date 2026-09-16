-- Name a slot's fence row without its retained state.json (ADR-0667, #2533).
--
-- Recovery has no incarnation string when state.json is absent or malformed, and the one
-- existing read is keyed on the worker's own credential, which cleanup_terminated unlinks.
-- SlotState.incarnation is derived as 'local-systemd:<unit>:<generation>', so a fixed slot
-- yields an exact prefix. Read-only: this releases nothing, and the caller still passes the
-- binding it returns to terminate_worker_incarnation unchanged.
CREATE FUNCTION public.recoverable_worker_incarnations(p_unit text)
RETURNS TABLE (incarnation text, authority_binding jsonb, fence_protocol integer)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_prefix text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_lifecycle_witness', 'member') THEN
        RAISE EXCEPTION 'lifecycle witness authority is required' USING ERRCODE = '42501';
    END IF;
    -- The unit is derived from the fixed slot index, never from a request. Validating the
    -- shape keeps the prefix free of pattern metacharacters, so the match below stays narrow
    -- by construction rather than by correct escaping.
    IF p_unit IS NULL OR p_unit !~ '^kdive-live-worker@[1-8]\.service$' THEN
        RAISE EXCEPTION 'recoverable lookup requires a fixed worker unit'
            USING ERRCODE = '22023';
    END IF;
    v_prefix := 'local-systemd:' || p_unit || ':';
    RETURN QUERY
    SELECT w.incarnation, w.authority_binding, w.fence_protocol
    FROM public.worker_incarnations AS w
    WHERE starts_with(w.incarnation, v_prefix)
      -- The generation is exactly 32 lowercase hex characters, so an incarnation that only
      -- extends the prefix is excluded by length before the pattern is applied.
      AND octet_length(w.incarnation) = octet_length(v_prefix) + 32
      AND substr(w.incarnation, octet_length(v_prefix) + 1) ~ '^[0-9a-f]{32}$'
      AND w.authority_kind = 'local'
      AND w.state = 'active'
    ORDER BY w.recorded_at, w.incarnation
    LIMIT 17;
END
$$;

REVOKE ALL ON FUNCTION public.recoverable_worker_incarnations(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.recoverable_worker_incarnations(text)
    FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.recoverable_worker_incarnations(text)
    TO kdive_lifecycle_witness;
