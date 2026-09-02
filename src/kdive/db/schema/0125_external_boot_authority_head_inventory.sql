-- Bounded authority journal inventory and worker peer authentication (ADR-0584, #2150).

CREATE FUNCTION public.list_external_boot_authority_journal_heads(
    p_authority_instance text
) RETURNS TABLE (
    authority_instance text,
    system_id uuid,
    sequence bigint,
    digest text,
    phase text,
    authority_id uuid,
    generation bigint,
    operation_identity text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
STABLE
AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_authority_instance IS NULL
       OR octet_length(p_authority_instance) NOT BETWEEN 1 AND 255 THEN
        RAISE EXCEPTION 'authority instance is invalid' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT
        head.authority_instance,
        head.system_id,
        head.sequence,
        head.digest,
        head.phase,
        head.authority_id,
        head.generation,
        head.operation_identity
    FROM public.external_boot_authority_journal_heads AS head
    WHERE head.authority_instance = p_authority_instance
    ORDER BY head.system_id
    LIMIT 4097;
END
$$;

CREATE FUNCTION public.authenticate_external_boot_authority_peer(
    p_credential_hash bytea
) RETURNS TABLE (peer_incarnation_id text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
STABLE
AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_credential_hash IS NULL OR octet_length(p_credential_hash) <> 32 THEN
        RAISE EXCEPTION 'worker credential hash is invalid' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT worker.incarnation
    FROM public.worker_incarnations AS worker
    WHERE worker.credential_hash = p_credential_hash
      AND worker.state = 'active'
      AND worker.fence_protocol = 4;
END
$$;

REVOKE ALL ON FUNCTION
    public.list_external_boot_authority_journal_heads(text),
    public.authenticate_external_boot_authority_peer(bytea)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
    kdive_provider_authority;

GRANT EXECUTE ON FUNCTION
    public.list_external_boot_authority_journal_heads(text),
    public.authenticate_external_boot_authority_peer(bytea)
TO kdive_provider_authority;
