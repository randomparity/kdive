-- Exact server-side release dispatch binding (#2204).

CREATE FUNCTION public.resolve_external_boot_release_dispatch_binding(
    p_activation_id uuid,
    p_system_id uuid,
    p_run_id uuid,
    p_plan_identity text
) RETURNS TABLE (provider_kind text, authority_instance text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = ''
STABLE
AS $$
    SELECT authority.provider_kind, authority.authority_instance
    FROM public.external_boot_activations AS activation
    JOIN public.external_boot_authorities AS authority
      ON authority.activation_id = activation.id
     AND authority.system_id = activation.system_id
     AND authority.run_id = activation.run_id
     AND authority.plan_identity = activation.plan_identity
    WHERE activation.id = p_activation_id
      AND activation.system_id = p_system_id
      AND activation.run_id = p_run_id
      AND activation.plan_identity = p_plan_identity
      AND authority.state IN ('current', 'retired')
    ORDER BY authority.generation DESC
    LIMIT 1
$$;

REVOKE ALL ON FUNCTION public.resolve_external_boot_release_dispatch_binding(
    uuid, uuid, uuid, text
) FROM PUBLIC, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
    kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.resolve_external_boot_release_dispatch_binding(
    uuid, uuid, uuid, text
) TO kdive_server;
