# Reusable-build pin recovery

An install worker holds a durable reusable-build pin while it may read kernel artifacts. A stale
job lease does not prove that worker stopped, so the reconciler and garbage collector do not remove
the pin automatically.

Use `ops.build_uses_list` with a token carrying platform operator and at least viewer on each intended
project to inspect at most 100 oldest-first pins per request. The server reads them only through the
database-capped, project-filtered diagnostic function. Platform authority alone returns an empty list;
it never grants tenant-data access. When `data.truncated` is true, pass the opaque
`data.next_cursor` back as `cursor` and continue until a page returns `data.truncated=false` and
`data.next_cursor=null`. This row-count limit is per request and has no reference clock; higher values
are clamped, the service may inspect one additional tenant-scoped row to establish truncation, and
following the cursor reaches later pins even when every pin on an earlier page remains active. A
malformed or wrong-tool cursor returns
`configuration_error` with `data.reason=invalid_cursor`; retry with the last returned cursor, or omit
it to restart at the oldest pin. A valid cursor past rows that were removed returns an empty terminal
page. Recovery is
advertised only when `KDIVE_WORKER_DEATH_VERIFIER` selects an authoritative
deployment verifier. `local` verifies hostname, boot ID, PID, and process start time against the
server host's `/proc`. `docker` verifies the worker's immutable container ID through the reference
Compose stack's inspect-only socket proxy. `kubernetes` verifies the worker Pod UID through the
chart's namespaced, resource-name-bounded `get pods` Role. Each verifier refuses a live
incarnation, malformed identity, authority error, or unverifiable state. The caller cannot supply
the death evidence, and heartbeat or lease age is never accepted as proof.

Docker daemon absence and Kubernetes Pod absence or same-name UID replacement are not death
proof: force removal, lost runtime state, or a control-plane partition can hide an object while its
process remains alive. Docker recovery therefore requires an inspectable stopped container;
Kubernetes recovery requires the same Pod UID in `Failed` or `Succeeded` phase. A container restart
inside a still-running Pod deliberately retains the Pod incarnation and keeps the old pin fenced;
this can retain artifacts until that exact Pod reaches a terminal phase, but cannot authorize early
deletion. Likewise, recover before deleting a terminal Pod because a later 404 fails closed.

Pass the exact `use_id` and `holder` returned by the list tool plus a concise operator reason. A
successful recovery atomically retains the generated evidence and reason in
`investigation_build_use_recoveries`, writes the platform audit row, and deletes only that use pin.
The server has no direct mutation authority: the same request transaction invokes the exact
role-gated, termination-evidence-checking function before it writes the platform audit row. Holder and
reason are each bounded to 512 UTF-8 bytes in the API and database. Repeat listing before recovery if
the holder may have changed. Recovery outside the caller's viewer-granted projects has the same
refusal shape as a missing use and leaves the pin unchanged.

For a worker-fence upgrade, migrate the roles and fence protocol, then use the deployment-specific
authority sequence:

- **Kubernetes:** rotate the distinct server, worker, reconciler, and lifecycle-witness credentials;
  start and verify the dedicated lifecycle-witness before starting current workers.
- **Compose:** rotate the distinct server, worker, reconciler, and lifecycle-witness credentials
  used by the lifecycle recipes. Use the operator-side lifecycle wrapper to gate current workers;
  Compose has no persistent lifecycle-witness service.

Verify registered current incarnations and recovery-tool exposure before resuming queue processing.
Rollback cannot restore old-worker claiming after the protocol migration, so recover forward with a
current image. Raw Docker/Compose commands, Pod force deletion, manual finalizer removal, and
database-owner or manual SQL bypasses retain pins; they do not authorize recovery.

When running the processes outside the reference Compose or Helm deployments, set both the worker
identity kind and a matching server verifier only after supplying equivalent authority. Leave the
verifier unset to omit both recovery tools from the MCP surface. Never expose the Compose proxy
port outside its internal network or grant the Helm server service account list, watch, update, or
delete permission on Pods.
