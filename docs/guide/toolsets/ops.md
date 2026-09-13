# ops toolset

The `ops` namespace is the platform-maintenance lane: reconciliation, bounded diagnostics,
capacity tuning, recovery, and accountable operational history. Use it only with the platform role
and confirmation requirements named by each tool schema.

## Observe and diagnose

- `ops.diagnostics` gathers bounded provider diagnostics for a platform operator.
- `ops.export_cost_classes` exports the active cost-class configuration for review or controlled
  transfer.
- `ops.export_systems_toml` exports the reconciled systems inventory for a platform operator.
- `ops.jobs_list` lists platform jobs to diagnose queue or worker state.
- `ops.build_uses_list` lists tracked build-use records before attempting recovery.
- `ops.tool_trail` reads the cross-tenant tool trail for a `platform_auditor`.

## Reconcile and tune

- `ops.reconcile_now` requests a platform reconciliation pass when observed state needs a fresh
  repair attempt.
- `ops.reconcile_systems` runs the system-specific reconciliation path; it requires
  `platform_admin` because it can repair durable state.
- `ops.set_cost_class_coeff` changes a cost-class coefficient for a platform operator.
- `ops.set_host_capacity` changes a host's declared capacity for a platform operator.
- `ops.set_queue_paused` pauses or resumes platform queue intake for a platform operator.

## Recover destructive exceptions

- `ops.recover_build_use` recovers an eligible build-use record after confirming the recorded
  state and recovery preconditions.
- `ops.resolve_recovery_orphan` resolves an external-boot recovery orphan and requires
  `platform_admin`.
- `ops.force_release` releases capacity through the break-glass path and requires
  `platform_admin` plus its explicit confirmation.
- `ops.force_teardown` tears down a system through the break-glass path and requires
  `platform_admin` plus its explicit confirmation.

Start with observation and the returned recovery guidance. Force operations are exceptional:
confirm the target, tenant impact, and cleanup evidence before using them.
