-- Let the worker create the per-System bootstrap key its provision handler owns (#1926).
-- Migration 0107 granted SELECT+DELETE but omitted the INSERT used by
-- ensure_system_bootstrap_key, leaving every least-privilege provision job unable to start.
GRANT INSERT ON TABLE public.system_bootstrap_keys TO kdive_worker;
