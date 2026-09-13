# Worker-handler write baseline

The test-owned [worker write baseline](../../tests/jobs/worker_write_baseline.json) records the
database writes reachable from active worker handlers. Its format is versioned; version 1 is
checked by [its structural test](../../tests/jobs/test_worker_write_baseline.py) and is not loaded
by the running service.

Each handler has an entry, including an empty `writes` list when the reviewed path has no database
write. A write records its table and operation, the handler call site, the SQL or function body,
the worker role, its direct or `SECURITY DEFINER` route, and both authority and grant evidence.
`covered` means the cited worker table grant covers the direct statement. `definer-mediated` means
the worker invokes the cited `SECURITY DEFINER` function through its EXECUTE grant. `LEAK` is an
explicit direct path whose cited grant matrix does not grant the needed table operation. The sorted
`confirmed_leaks` list is exactly the `LEAK` ids; it is evidence, not permission to change a role.

The current sweep is limited to `kdive_worker` handler paths. It excludes server and reconciler
paths, live grant probing, and remediation. Evidence begins with the worker grant matrix in
`0107_process_role_data_access.sql`, then uses the narrower worker grants and revocations in
`0114_host_dump_volume_leases.sql`, `0117_worker_bootstrap_key_insert.sql`,
`0118_worker_audit_log_insert.sql`, `0121_external_boot_activations.sql`,
`0126_remote_module_attempt_obligations.sql`, `0138_external_boot_recovery_quarantine.sql`, and
`0152_worker_system_mutation_discharge.sql`.

When an intentional handler-write change lands, trace its handler call through the repository or
definer function, update the affected JSON records and source fragments, retain a no-write entry
when appropriate, and run:

```sh
uv run python -m pytest tests/jobs/test_worker_write_baseline.py -q
```

Use [ADR-0649](../adr/0649-worker-handler-write-baseline.md) for the decision boundary. File a
separate remediation item for a `LEAK`; this baseline does not alter grants, schema, or runtime
behavior.
