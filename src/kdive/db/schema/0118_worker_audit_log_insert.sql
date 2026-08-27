-- 0118_worker_audit_log_insert.sql — append-only worker transition auditing.
GRANT INSERT ON TABLE public.audit_log TO kdive_worker;
GRANT SELECT (id) ON TABLE public.audit_log TO kdive_worker;
