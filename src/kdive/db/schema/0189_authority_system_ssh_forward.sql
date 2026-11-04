-- 0189_authority_system_ssh_forward.sql -- #3536 / ADR-0811 (forward-only, ADR-0015).
-- An authority-owned System's guest SSH forward lives in the authority's private libvirt daemon,
-- so the server cannot read it the way it reads a fixed-worker System's domain XML.  The authority
-- now reports the loopback port in its `provision-ready` receipt; the finalizer and the reconciler
-- repair copy it onto the ownership row, and the server-readable binding returns it.

ALTER TABLE public.authority_system_ownership
    ADD COLUMN ssh_forward_port integer
    CHECK (ssh_forward_port BETWEEN 1024 AND 65535);

-- The receipt is canonical JSON the authority signed into the journal.  The port counts only when
-- the receipt names this System and a `provision-ready` disposition; anything else, including a
-- receipt written before the field existed, yields NULL and the server fails closed.
CREATE FUNCTION public.authority_system_receipt_ssh_forward_port(
    p_receipt bytea, p_system_id uuid
) RETURNS integer LANGUAGE plpgsql IMMUTABLE SET search_path = '' AS $$
DECLARE
    v_receipt jsonb;
    v_port text;
BEGIN
    v_receipt := convert_from(p_receipt, 'UTF8')::jsonb;
    IF jsonb_typeof(v_receipt) IS DISTINCT FROM 'object'
       OR v_receipt->>'disposition' IS DISTINCT FROM 'provision-ready'
       OR v_receipt->>'system_id' IS DISTINCT FROM p_system_id::text
       OR jsonb_typeof(v_receipt->'ssh_forward_port') IS DISTINCT FROM 'number' THEN
        RETURN NULL;
    END IF;
    v_port := v_receipt->>'ssh_forward_port';
    IF v_port !~ '^[0-9]{4,5}$' OR v_port::integer NOT BETWEEN 1024 AND 65535 THEN
        RETURN NULL;
    END IF;
    RETURN v_port::integer;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION public.authority_system_receipt_ssh_forward_port(bytea, uuid)
    FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
         kdive_provider_authority;

DO $$
DECLARE
    v_definition text;
    v_old constant text := $old$UPDATE public.authority_system_ownership SET state='ready' WHERE system_id=v_owner.system_id;$old$;
    v_new constant text := $new$UPDATE public.authority_system_ownership SET state='ready',
            ssh_forward_port=public.authority_system_receipt_ssh_forward_port(
                v_attempt.receipt_bytes,v_owner.system_id)
        WHERE system_id=v_owner.system_id;$new$;
BEGIN
    SELECT pg_get_functiondef(
        'public.finalize_authority_system_attempt(bytea,uuid,integer,uuid,bigint,bigint,text,bytea)'
        ::regprocedure
    ) INTO v_definition;
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1 THEN
        RAISE EXCEPTION 'authority System finalizer ready shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;

DO $$
DECLARE
    v_definition text;
    v_old constant text := $old$            UPDATE public.authority_system_ownership SET state='ready'
            WHERE system_id=v_attempt.system_id;$old$;
    v_new constant text := $new$            UPDATE public.authority_system_ownership SET state='ready',
                ssh_forward_port=public.authority_system_receipt_ssh_forward_port(
                    v_attempt.receipt_bytes,v_attempt.system_id)
            WHERE system_id=v_attempt.system_id;$new$;
BEGIN
    SELECT pg_get_functiondef(
        'public.repair_terminal_authority_system_attempts(integer)'::regprocedure
    ) INTO v_definition;
    IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1 THEN
        RAISE EXCEPTION 'authority System repair ready shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;

-- The return type grows, so the function is replaced rather than altered.  The port is exposed
-- only while the System can be attached to: ready or activated.
DROP FUNCTION public.resolve_authority_system_control_binding(uuid);

CREATE FUNCTION public.resolve_authority_system_control_binding(p_system_id uuid)
RETURNS TABLE(
    system_id uuid, allocation_id uuid, resource_id uuid, provider_kind text,
    resource_name text, authority_instance text, profile_identity text,
    root_identity text, ownership_state text, ssh_forward_port integer
) LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT owner.system_id,owner.allocation_id,owner.resource_id,owner.provider_kind,
           owner.resource_name,owner.authority_instance,owner.profile_identity,
           owner.root_identity,owner.state,
           CASE WHEN owner.state IN ('ready','activated') THEN owner.ssh_forward_port END
    FROM public.authority_system_ownership AS owner
    WHERE (pg_has_role(session_user,'kdive_server','member')
           OR pg_has_role(session_user,'kdive_reconciler','member'))
      AND owner.system_id=p_system_id
$$;

REVOKE ALL ON FUNCTION public.resolve_authority_system_control_binding(uuid)
    FROM PUBLIC, kdive_worker, kdive_lifecycle_witness, kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.resolve_authority_system_control_binding(uuid)
    TO kdive_server, kdive_reconciler;
