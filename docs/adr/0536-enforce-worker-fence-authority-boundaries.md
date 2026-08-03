# 0536 — Enforce worker-fence authority at process, socket, and foreign-key boundaries

## Status

Accepted (2026-08-03)

## Context

ADR-0533 separated worker-fence database capabilities, but deployment and schema composition still
left three bypasses. Kubernetes mounted the lifecycle-witness principal, envelope key, TLS private
key, and Pod API token into the reconciler process. The credential broker's application semaphore
ran only after `asyncio` completed TLS, so incomplete handshakes were outside its concurrent-session
limit. Finally, `investigation_build_uses.job_id ON DELETE CASCADE` let routine reconciler job
cleanup delete a live pin without terminal evidence or a recovery audit.

These are enforcement failures rather than documentation gaps. Distinct PostgreSQL roles do not
separate authority when one process receives both credentials; a post-handshake limit does not bound
pre-handshake allocations; and an audited deletion function is not exclusive while a cascading
foreign key supplies another deletion path.

## Decision

Run Kubernetes lifecycle authority as a dedicated `lifecycle-witness` process and Deployment. Only
that workload receives the lifecycle-witness database ref, broker TLS private key, credential
envelope key, projected service-account token, Pod-read/finalizer-patch RBAC, and TokenReview
authority. The reconciler uses a distinct service account without an API token and receives none of
the witness secrets. The broker Service and ingress NetworkPolicy select only the witness Pod.

Admit raw broker connections before allocating TLS. A fixed set of 64 listener workers accepts at
most one socket each and upgrades only those admitted sockets to TLS. The count is concurrent
connections per broker listener with no reference clock. The kernel listen backlog is also bounded;
connections beyond admitted and queued capacity are refused or wait outside broker tasks. Each
admitted handshake retains the five-second timeout and each complete exchange retains the
15-second timeout. A refused or timed-out init client retries the same broker operation.

Replace the `investigation_build_uses.job_id` cascading foreign key in a monotonic migration with
`ON DELETE RESTRICT`. A referenced job cannot be removed until the credential-owning worker releases
the exact use or evidence-backed recovery removes it and writes the permanent audit row.

This decision tightens the deployment and database enforcement of ADR-0533 and ADR-0535; it does not
change their role capabilities, evidence definition, tenant scope, or recovery API.

## Consequences

The Kubernetes control plane has four long-running workloads rather than three. Operators must
monitor the witness's dedicated readiness endpoint and provision its authority Secrets separately
from reconciler configuration. A witness outage retains worker finalizers and blocks new worker
credential delivery, which is the fail-closed outcome.

Incomplete TLS peers consume only the fixed admitted socket/task budget and bounded kernel backlog.
When the budget recovers, a valid init client can connect without restarting the witness.

Routine job cleanup can now fail with a foreign-key violation while an attempt still holds a build
use. Cleanup must retry after normal release or evidence recovery. Recovery audit rows retain their
job identity after the job itself is subsequently deleted.

## Considered & rejected

- **Keep witness authority inside the reconciler with a second DSN.** A process compromise can use
  every credential mounted into that process, so distinct database roles do not form a boundary.
- **Rely on the application semaphore after `asyncio.start_server` TLS.** The callback begins after
  the handshake and cannot bound incomplete handshake tasks or sockets.
- **Use timeout alone as the TLS control.** A timeout bounds duration per connection, not concurrent
  allocation; a compromised worker can continuously replace timed-out handshakes.
- **Retain cascading job deletion and guard only repository code.** The reconciler role can delete
  jobs directly, and PostgreSQL would still erase the pin outside evidence-backed recovery.
- **Delete recovery audit rows with jobs.** Audit evidence is permanent and must retain the original
  job identifier even after ordinary job retention expires.
