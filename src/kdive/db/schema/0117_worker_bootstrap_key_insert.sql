-- 0117_worker_bootstrap_key_insert.sql — worker-owned per-System bootstrap-key creation.
GRANT INSERT ON TABLE public.system_bootstrap_keys TO kdive_worker;
