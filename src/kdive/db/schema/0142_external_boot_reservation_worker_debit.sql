-- Worker-scoped external-boot recovery capacity debit (ADR-0613).
CREATE FUNCTION public.mark_external_boot_reservation_ready(
    p_credential_hash bytea, p_job_id uuid, p_job_attempt integer,
    p_activation_id uuid, p_system_id uuid, p_operation_owner_id uuid,
    p_authority_generation bigint, p_store_identity text, p_reserve_bytes bigint,
    p_recovery_max_bytes bigint
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_used bigint;
    v_updated integer;
BEGIN
    IF p_recovery_max_bytes <= 0 OR p_reserve_bytes <= 0
       OR p_reserve_bytes > p_recovery_max_bytes THEN
        RAISE EXCEPTION 'invalid external boot recovery geometry' USING ERRCODE = '22023';
    END IF;
    PERFORM 1 FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    PERFORM 1 FROM public.external_boot_authorities
      WHERE job_id = p_job_id AND job_attempt = p_job_attempt FOR UPDATE;
    PERFORM 1 FROM public.external_boot_reservations
      WHERE activation_id = p_activation_id FOR UPDATE;
    IF NOT EXISTS (
        SELECT 1 FROM public.jobs j
        JOIN public.worker_incarnations w ON w.incarnation = j.worker_id
        JOIN public.external_boot_authorities a
          ON a.job_id = j.id AND a.job_attempt = j.attempt
        JOIN public.external_boot_activations x
          ON x.id = a.activation_id
        JOIN public.external_boot_reservations r ON r.activation_id = x.id
        WHERE j.id = p_job_id AND j.attempt = p_job_attempt AND j.state = 'running'
          AND j.lease_expires_at > clock_timestamp()
          AND w.state = 'active' AND w.fence_protocol = 4
          AND w.credential_hash = p_credential_hash AND x.id = p_activation_id
          AND a.state = 'current' AND a.worker_incarnation = j.worker_id
          AND x.system_id = p_system_id
          AND x.operation_owner_id = p_operation_owner_id
          AND x.authority_generation = p_authority_generation
          AND NOT EXISTS (
              SELECT 1 FROM public.external_boot_authorities newer
              WHERE newer.system_id = a.system_id AND newer.generation > a.generation
          )
          AND x.state = 'preparing' AND NOT x.cleanup_complete
          AND r.store_identity = p_store_identity AND r.reserved_bytes = p_reserve_bytes
          AND r.state IN ('pending', 'ready')
    ) THEN
        RETURN 'superseded';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.external_boot_reservations
        WHERE activation_id = p_activation_id AND state = 'ready'
    ) THEN
        RETURN 'applied';
    END IF;
    SELECT coalesce(sum(reserved_bytes), 0) INTO v_used
      FROM public.external_boot_reservations
     WHERE store_identity = p_store_identity AND state = 'ready';
    IF v_used + p_reserve_bytes > p_recovery_max_bytes THEN
        RETURN 'capacity_exhausted';
    END IF;
    UPDATE public.external_boot_reservations
       SET state = 'ready', ready_at = clock_timestamp()
     WHERE activation_id = p_activation_id AND state = 'pending';
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    IF v_updated <> 1 THEN
        RETURN 'superseded';
    END IF;
    RETURN 'applied';
END;
$$;
REVOKE ALL ON FUNCTION public.mark_external_boot_reservation_ready(
    bytea, uuid, integer, uuid, uuid, uuid, bigint, text, bigint, bigint
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.mark_external_boot_reservation_ready(
    bytea, uuid, integer, uuid, uuid, uuid, bigint, text, bigint, bigint
) TO kdive_worker;
