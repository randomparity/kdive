-- Runtime roles are separate non-login capabilities (ADR-0533, #1803).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kdive_server') THEN
        CREATE ROLE kdive_server NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kdive_worker') THEN
        CREATE ROLE kdive_worker NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kdive_reconciler') THEN
        CREATE ROLE kdive_reconciler NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kdive_lifecycle_witness') THEN
        CREATE ROLE kdive_lifecycle_witness NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON TABLE public.worker_incarnations FROM PUBLIC;
REVOKE ALL ON TABLE public.investigation_build_uses FROM PUBLIC;
REVOKE ALL ON TABLE public.investigation_build_use_recoveries FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO kdive_server, kdive_worker, kdive_reconciler,
    kdive_lifecycle_witness;
