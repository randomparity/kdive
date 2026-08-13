# Supervised capture operations implementation plan

Goal: make every `capture_traffic` provider phase a durable, terminable child operation and install
the unconditional offline protocol-3 cutover authorized for KDIVE's pre-release deployment.

Architecture: the worker creates a `launching` row before exec, releases a one-byte gate only after
exact Linux identity commits, and consumes output only after exact-process absence plus a fresh
provider ordering/absence probe. Security-definer database functions fence operation transitions,
recovery, retries, and the offline protocol cutover. Local and remote libvirt share the operation
protocol but retain separate execution assembly and live ordering proofs.

Tech stack: Python 3.14, psycopg/PostgreSQL migrations, asyncio subprocess/pidfd, Linux seccomp,
libvirt/QMP, pytest, Helm/Compose deployment documentation.

## Global constraints

- Branch `feat/supervise-capture-operations-1951`; base `main`.
- Effective targets are x86_64 and ppc64le; the x86_64 host is included.
- No new dependency: use the Linux `seccomp` syscall through a small `ctypes` wrapper and the
  standard-library `os.pidfd_open`/`signal.pidfd_send_signal` surfaces available on Python 3.14.
- Child argv is exactly `python -m kdive capture-operation --launch-token <token> --gate-fd <fd>`;
  cwd is the private attempt directory and request basename is `request.json`.
- The child is single-task after exec. Before the gate read, install a filter denying `fork`,
  `vfork`, `clone`, `clone3`, `execve`, and `execveat`; provider imports occur after release.
- Gate-release authority check is `SELECT 1` on the lock session immediately before the write;
  recurring probes run every 250 ms with one-second client and statement timeouts.
- TERM and KILL waits are five seconds each on the monotonic clock, per cancellation request.
- Protocol cutover is offline and unconditional: no online drain, compatibility generation, or
  legacy-work preservation. Every protocol-2 incarnation needs exact lifecycle termination.
- #1951 writes `operation_quiescent` and `cutoff_at`; #1952 owns `publication_closed` and
  aggregate `complete`. #1946 may not select historical rows until aggregate completion.
- No MCP tool/schema change. Packet bytes and credentials never enter argv, JSON logs, or Postgres.
- Run focused tests during TDD, then `just ci`. Live local/remote proofs remain gated and are
  reported separately rather than treated as passing when fixtures are absent.

## Task 1: Persist and fence capture operations

Files:

- Create `src/kdive/db/schema/0112_capture_operation_supervision.sql`.
- Create `src/kdive/jobs/capture_operations/repository.py` and package initializer.
- Modify `src/kdive/services/runs/worker_incarnations.py`, `src/kdive/jobs/queue.py`, and
  `src/kdive/db/schema/0106_worker_fence_protocol_claim.sql` only through migration replacements,
  never by editing applied migrations.
- Create `tests/jobs/test_capture_operation_repository.py` and
  `tests/jobs/test_capture_operation_cutover.py`.

Interfaces:

- `CaptureOperationState = Literal["launching", "gated", "running", "cancel_requested", "exited"]`.
- `CaptureOperationIdentity(host_instance, boot_id, pid, start_ticks)`.
- `create_launching(conn, credential, job_id, attempt, snapshot) -> CaptureOperation`.
- `record_identity(...)`, `mark_running(...)`, `request_cancel(...)`, and
  `acknowledge_exit(...) -> CaptureOperation`.
- `recover_operation(conn, replacement_credential, operation_id, evidence) -> CaptureOperation`.
- `CURRENT_WORKER_FENCE_PROTOCOL = 3`.

Steps:

1. Add failing migration tests for uniqueness, transition edges, exact-owner writes, authorized
   same-boundary/successor recovery, cross-boundary denial, and retry refusal while prior evidence
   is incomplete. Expected: relations/functions are absent.
2. Add failing cutover tests for the global registration lock, protocol-2 restart races, missing
   termination, residual running-job cancellation, queued-job preservation, operation-only cutoff,
   and fresh-install field values. Expected: migration 0112 is absent.
3. Implement the tables, constraints, indexes, security-definer functions, grants, protocol-3
   registration/claim replacements, and repository types. All transitions use database-clock
   timestamps and fixed lock ordering.
4. Run `uv run python -m pytest tests/jobs/test_capture_operation_repository.py
   tests/jobs/test_capture_operation_cutover.py -q` and expect all pass.
5. Run `just migration-order-check`, `just schema-guard main`, `just type`, and commit
   `feat(jobs): persist supervised capture operations`.

Acceptance: one current operation per charged attempt; no later attempt while prior evidence is
incomplete; recovery cannot cross authority scope; the offline transaction rejects every unsafe
legacy state and leaves aggregate completion false.

## Task 2: Implement the gated Linux child boundary

Files:

- Create `src/kdive/jobs/capture_operations/protocol.py`, `linux_identity.py`, `sandbox.py`,
  `child.py`, and `launcher.py`.
- Modify `src/kdive/__main__.py` to add the internal `capture-operation` process verb.
- Create matching tests under `tests/jobs/capture_operations/`.

Interfaces:

- `CaptureRequest` and `CaptureResult` strict Pydantic models with canonical JSON helpers.
- `LinuxIdentity.read(pid)`, `open_pidfd()`, `signal()`, and `is_absent()`.
- `GatedCaptureLauncher.launch(request, operation) -> LaunchedCapture`.
- `LaunchedCapture.release()`, `wait()`, and `cancel()`.
- `run_capture_child(launch_token: str, gate_fd: int) -> int`.

Steps:

1. Write a real helper-process test proving no marker appears before release, gate EOF exits, exact
   argv/cwd are used, and forbidden parent environment variables are absent. Confirm red.
2. Add red fault tests for spawn, stat, pidfd, identity-write, release-write, TERM timeout, KILL
   timeout, token scan, unreadable `/proc`, result bounds, modes, symlinks, and malformed JSON.
3. Implement private directories/files with directory-fd-relative `O_NOFOLLOW` opens, canonical
   digests, allowlisted environment, launch-token recovery, pidfd signaling, and bounded waits.
4. Implement the pre-gate seccomp filter and tests that every denied syscall returns `EPERM` and
   `/proc/<pid>/task` remains a singleton on x86_64 and ppc64le. The ppc64le arm uses the native
   POWER carrier documented by `docs/operating/runbooks/power-host-bringup.md`: after that runbook's
   environment setup, run `uv run python -m pytest
   tests/jobs/capture_operations/test_sandbox.py -q`. Success means all six denied syscalls return
   `EPERM` and the task directory stays singleton. This is a required release proof; if no native
   POWER host is available, report the arm unavailable and do not claim cross-platform completion.
5. Run `uv run python -m pytest tests/jobs/capture_operations -q`, `just lint`, and `just type`;
   expect green. Commit `feat(jobs): add gated capture child boundary`.

Acceptance: provider/request input is unreachable before release; an identity-less launch remains
discoverable; exact-child cancellation is PID-reuse safe; the child cannot create another task.

## Task 3: Move provider execution and quiescence behind the boundary

Files:

- Extend `src/kdive/providers/ports/traffic.py` with request execution and quiescence protocols.
- Modify local and remote traffic-capture implementations and composition.
- Create provider-focused unit tests and gated live tests in the matching provider/live trees.

Interfaces:

- `TrafficCaptureExecutor.execute(request, result_dir) -> CaptureExecutionResult`.
- `TrafficCaptureQuiescence.prove_absent(resource_id, domain_name, qom_id) -> QuiescenceEvidence`.
- Local composition reconstructs from allowlisted URI; remote composition resolves the exact
  Resource-bound TLS configuration without a worker database credential.

Steps:

1. Add red unit tests that probes reconnect, detach idempotently, query the exact QOM id, reject
   presence/unreachable/unordered transports, and redact details.
2. Add red child integration tests for each provider fake covering every provider-method failure,
   result bounds, detach/reclaim, and no descendant tasks.
3. Implement synchronous child executors and independent quiescence probes without sharing code
   across provider families beyond the port models.
4. Add gated local and remote real-stack tests delaying an accepted monitor mutation, killing the
   child, and proving the new connection cannot acknowledge early.
5. Run the focused unit tests; run `just test-live` and the remote live recipe only when fixtures
   are present, reporting skips as unavailable rather than proof. Run `just lint` and `just type`.
6. Commit `feat(providers): prove capture operation quiescence`.

Acceptance: each real provider has a falsifiable cross-connection ordering proof and cannot emit
quiescence evidence from process absence alone.

## Task 4: Integrate supervision with worker authority loss

Files:

- Modify `src/kdive/jobs/handlers/control/capture_traffic.py`, `src/kdive/jobs/worker.py`,
  `src/kdive/jobs/assembly.py`, and `src/kdive/processes/worker.py`.
- Add handler, worker, and startup-recovery tests in matching test modules.

Interfaces:

- `CaptureOperationSupervisor.execute(conn, job, snapshot, request) -> bytes | None`.
- `recover_capture_operations(pool, resolver, host_identity, credential) -> RecoverySummary`.

Steps:

1. Add red worker tests for heartbeat false/error, lock loss before release, immediately after
   release, stalled probe, cancellation race, worker stop, and surviving both signal waits.
2. Add red retry tests proving an unacknowledged prior attempt is not charged or hidden.
3. Replace provider `to_thread` calls in the handler with supervisor execution while retaining BPF
   validation/trim and the existing artifact path owned by #1952.
4. Race capture dispatch with heartbeat authority, implement the release-time and recurring lock
   probes, and run startup recovery before readiness/claim loops begin.
5. Run focused handler/worker/process tests, `just lint`, and `just type`; expect green. Commit
   `feat(worker): supervise capture operations`.

Acceptance: authority loss starts bounded cancellation; responsive children are absent before
return; unresponsive children remain durably barred for startup recovery; publication behavior is
unchanged.

## Task 5: Wire the unconditional deployment cutover

Files:

- Create `scripts/live-stack/cutover-capture-protocol.sh` for the host-process/Compose path.
- Create `scripts/cutover-capture-protocol-helm.sh` for Helm deployments.
- Modify `deploy/helm/kdive/templates/job-migrate.yaml` so migration does not race a worker rollout,
  and document the replacing upgrade in `deploy/helm/kdive/README.md` and
  `docs/operating/runbooks/kubernetes-deploy.md`.
- Modify `scripts/live-stack/README.md`; add shell/shape guards in
  `tests/scripts/test_live_stack_scripts.py`, `tests/scripts/test_live_workflow_shape.py`,
  `tests/helm/test_helm_render.py`, and `tests/helm/test_helm_upgrade_config.py`.
- Flip ADR-0558 to Accepted only in the final implementation commit.

Steps:

1. Add red tests that both cutover scripts stop all workers, refuse rolling protocol 2→3, preserve
   the original replica count, surface exact blocking incarnation/job diagnostics, and permit a
   fresh protocol-3 installation.
2. Implement the local sequence as `stop_daemons` → verify every recorded protocol-2 host PID is
   absent → `pg_dump --format=custom` → `python -m kdive migrate` →
   `restart_host_processes`. A failed termination precondition or migration leaves workers stopped
   and prints the exact recovery command; the script never calls `down.sh` or wipes backends.
3. Implement the Helm sequence with explicit `RELEASE`, `NAMESPACE`, values file, and backup path:
   read `.Values.worker.replicas`, `kubectl scale statefulset/${RELEASE}-worker --replicas=0`, wait
   for every worker pod deletion/finalizer termination witness, run `pg_dump --format=custom`, then
   run `helm upgrade` with the target image and original replica count. The migration hook acquires
   the global cutover lock and refuses any live protocol-2 incarnation before installing protocol
   3. Do not add a compatibility flag or alternate rolling path.
4. Make failures operationally explicit: a precondition failure leaves the old schema and stopped
   workers; a post-migration failure leaves protocol 3 installed and workers stopped. The only
   post-migration rollback is `pg_restore --clean --if-exists` of the named backup followed by the
   prior image/chart; never start a protocol-2 worker against the migrated database.
5. Update both runbooks with these exact commands and state that #1952 still gates aggregate
   historical coverage.
6. Run focused deployment tests, `just lint-shell`, `just lint-ansible`, `just test-ansible`,
   `just docs-links`, `just docs-paths`, and `just adr-status-check`.
7. Set ADR-0558 to Accepted, run `just ci`, verify `git status --porcelain` is empty after staging,
   and commit `feat(deploy): enforce capture protocol cutover`.

Acceptance: no protocol-2 worker can register, authenticate, claim, or survive into the cutoff;
residual running legacy captures are canceled only after positive owner termination; fresh and
upgrade installs leave aggregate completion false for #1952.

## Requirement map

- Durable attempt identity: Tasks 1–2.
- Identity before provider work: Tasks 1–2.
- Lock-session loss terminates: Task 4.
- Replacement recovers every launch state: Tasks 1–2 and 4.
- Provider-specific falsifiable quiescence: Task 3.
- Unconditional pre-release cutoff: Tasks 1 and 5.
- `just ci`: Task 5 after all focused gates.

Rollback: before deployment, revert the branch. After migration 0112, rollback is restore-from-
backup plus the prior binary because protocol 3 and operation rows are an intentional replacing
schema with no dual-format compatibility path. No production customer data exists, but operator
development data is still state and must not be silently discarded.
