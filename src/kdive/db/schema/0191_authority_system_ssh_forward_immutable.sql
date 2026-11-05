-- 0191_authority_system_ssh_forward_immutable.sql -- #3546 / ADR-0811 (forward-only, ADR-0015).
-- The server trusts authority_system_ownership.ssh_forward_port as the loopback endpoint for debug
-- attach, so it joins the ownership row's immutable fields once set.  The finalizer and the
-- reconciler repair write it on the provisioning-to-ready transition, from NULL, and a row never
-- returns to provisioning.  CREATE OR REPLACE keeps the 0149 trigger and revoked privileges.

CREATE OR REPLACE FUNCTION public.reject_authority_system_binding_update() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF (NEW.system_id, NEW.allocation_id, NEW.resource_id, NEW.provider_kind,
        NEW.resource_name, NEW.authority_instance, NEW.profile_identity,
        NEW.root_identity, NEW.created_at)
       IS DISTINCT FROM
       (OLD.system_id, OLD.allocation_id, OLD.resource_id, OLD.provider_kind,
        OLD.resource_name, OLD.authority_instance, OLD.profile_identity,
        OLD.root_identity, OLD.created_at) THEN
        RAISE EXCEPTION 'authority System ownership binding is immutable';
    END IF;
    IF OLD.bootstrap_identity IS NOT NULL
       AND NEW.bootstrap_identity IS DISTINCT FROM OLD.bootstrap_identity THEN
        RAISE EXCEPTION 'authority System bootstrap identity is immutable';
    END IF;
    IF OLD.first_activation_id IS NOT NULL
       AND NEW.first_activation_id IS DISTINCT FROM OLD.first_activation_id THEN
        RAISE EXCEPTION 'authority System first activation is immutable';
    END IF;
    IF OLD.ssh_forward_port IS NOT NULL
       AND NEW.ssh_forward_port IS DISTINCT FROM OLD.ssh_forward_port THEN
        RAISE EXCEPTION 'authority System SSH forward port is immutable';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END
$$;
