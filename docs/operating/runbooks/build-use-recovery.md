# Reusable-build pin recovery

An install worker holds a durable reusable-build pin while it may read kernel artifacts. A stale
job lease does not prove that worker stopped, so the reconciler and garbage collector do not remove
the pin automatically.

Use `ops.build_uses_list` with a platform-operator token to inspect a bounded oldest-first page of
pins. Recovery is advertised only when `KDIVE_WORKER_DEATH_VERIFIER` selects an authoritative
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
Both evidence and reason are bounded in the API and database. Repeat listing before recovery if the
holder may have changed.

When running the processes outside the reference Compose or Helm deployments, set both the worker
identity kind and a matching server verifier only after supplying equivalent authority. Leave the
verifier unset to omit both recovery tools from the MCP surface. Never expose the Compose proxy
port outside its internal network or grant the Helm server service account list, watch, update, or
delete permission on Pods.
