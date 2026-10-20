# Orchestration fault proofs on x86_64 libvirt

## Authority and scope

Issue #3117 under epic #2803 owns six cells that `scripts/coverage_campaign/contract.py` emits as
`failure-resource/<provider>/x86_64/<failure>`, with provider `local-libvirt` or
`remote-libvirt` and failure `worker-interruption`, `lost-response` or `backing-service`. Each
cell's assertions are `(<failure>, no-duplicates, protected-state, cleanup)`, its roles
`server, worker, reconciler`, and its inputs the six kernel input identities. Frozen charter:
WORK:SCOPE `q3117-029cd8e8`. Exclusions approved for this campaign (2026-10-16): low-resource
cells #3118, native POWER #2818, durable finalization and cross-replica guarantees #2681,
inspected-image registration #3218 (merged). This change adds test carriers, coverage bindings
and runbook text. It changes product source only under the conditional fence fix below. Any
other product defect a cell exposes is recorded as that cell's failure and routed to the
campaign root. It is never worked around in the carrier.

The operator settled D1–D4 on 2026-10-20 (WORK:SCOPE re-freeze #2), choosing option (a) for
each. The decisions are listed at the end of this spec.

## Source findings

- **Execution path.** `systems.provision` takes the authority path only when the resource
  carries an authority route (`src/kdive/services/systems/admission.py:444-447`). The demo-up
  lane (`deploy/ansible/playbooks/local-libvirt-host.yml`) installs no authority, so the managed
  host stack runs the direct `provision_handler` (`src/kdive/jobs/handlers/systems.py`). The
  authority lane is epic #3152, which is blocked. Its orphan and conflict construction (#3185)
  is in flight. The cells' declared roles do not include `authority`.
- **Job recovery.** A claim charges one attempt and sets a 5-minute lease
  (`src/kdive/jobs/queue.py:51-52`). The worker renews the lease every 30 s. Another worker
  reclaims a `running` row whose lease has lapsed while attempts remain. The reclaimed attempt
  re-runs the handler from the start. A late `complete` from the old attempt is fenced and
  logged as `job <id> completed but was reclaimed; result dropped`
  (`src/kdive/jobs/worker.py:627`). `KDIVE_WORKER_DEATH_VERIFIER` only decides whether the two
  build-use recovery tools are registered (`src/kdive/mcp/assembly/tool_registration.py:262`).
  It plays no part in job reclaim. These cells therefore do not need that lane, even though the
  issue text assumes they do.
- **Provision re-execution.** Local provisioning redefines the domain in place, treats an
  already running domain as success, and creates the overlay only when it is absent
  (`src/kdive/providers/local_libvirt/lifecycle/provisioning.py:296-301`). Remote provisioning
  fixes the domain name and UUID and reuses the overlay
  (`src/kdive/providers/remote_libvirt/lifecycle/provisioning.py:200-293`). That is the
  documented recovery outcome for a reclaimed provision. No advisory lock is held across the
  provider call (`systems.py:296-340`), so a paused holder does not block the reclaim.
- **Unfenced stale side effects.** When a stale attempt's provider call raises, three of its
  writes are not fenced against the reclaimed attempt:
  - the local teardown of the domain after `started` (`provisioning.py:383-386`);
  - the local reclaim of the overlay and baseline that attempt 1 created (`provisioning.py:387-391`);
  - `_record_system_failure`, which moves a `ready` System to `failed`. That transition is legal
    (`systems.py:321-334`, `src/kdive/domain/capacity/state.py:250-257`).

  Locally, the 900 s boot window outlasts the roughly 6-minute hold, so the stale attempt can
  still finish cleanly. Remotely, the paused worker's libvirt connection is expected to drop on
  keepalive. The resumed `wait_for_agent` would then raise
  (`src/kdive/providers/remote_libvirt/lifecycle/readiness.py:62-71`), and the System would be
  marked `failed`. That outcome is inferred from source and not run. If it happens, the remote
  worker-interruption cell records it as a product defect, routes it to the root, and does not
  pass. The carrier observes System state, domain and overlay separately after the stale
  attempt ends.
- **Idempotency.** A `systems.provision` key is stored per principal and tool, inside the
  admission transaction. The stored admission envelope is replayed unchanged. A failed
  admission stores nothing. The arguments are not compared (`provision.py:101-146`,
  `docs/guide/async-jobs.md:70-96`). The row stores
  `{"envelope": {"type": "provision_job", "job": <job>, "system_id": <id>}}`
  (`src/kdive/services/idempotency/envelope.py:103-105`, `admission.py:186-195`). The server
  builds its response directly after that commit (`provision.py:130-146`). So once the commit
  is visible, abandoning the connection and discarding a received envelope look the same to the
  server.
- **Backing services.** On a database outage the worker's claim loop and process survive
  (`worker.py:431-443`). The handler holds one pool connection for its whole run
  (`worker.py:503-509`). A stopped Postgres breaks that connection, so attempt 1 cannot commit.
  It recovers in one of two ways:
  - The attempt fails and is requeued as the retryable `infrastructure_failure`
    (`worker.py:848-852`).
  - If the fail write itself is lost, the lease lapses and the job is reclaimed
    (`worker.py:695-721`).

  A not-ready worker pauses dequeue
  rather than failing jobs (ADR-0090 §5). An object-store failure maps to the retryable
  `infrastructure_failure` with no backoff (`src/kdive/store/objectstore.py:68-82`). The worker
  readiness check includes the store, so new claims pause during an outage.
- **Worker hold.** `tests/live_vm/installed_local_authority_support.py` already pauses the
  exact job holder with SIGSTOP through a pidfd. It checks the slot's retained `state.json`,
  `MainPID`, `InvocationID` and `boot_id` first (`set_exact_worker_hold`), then observes the
  reclaim (`running_job_claim`, `wait_for_reclaimed_job`). SIGSTOP leaves systemd and the
  lifecycle witness untouched. An out-of-band `systemctl restart` or kill wedges the slot
  (`docs/operating/runbooks/live-stack.md:362-370`), so the carrier never uses one.

## Design

### Frame

The carrier reuses the merged #3118 frame in `tests/integration/live_stack/resource_limits.py`:
`Held` (an owned Allocation and its System), `Sampler` (allocation status, `in_use` and owned
domains), `Provider`/`LOCAL`/`remote_provider` (the worker's libvirt or the provider host's
libvirt over SSH), `on_allocation`, `registered`, `remote_guest`, `fixture`, `prove_held` and
`release`. It also reuses `run_cell` from `scenario.py`, which reads the deployed identity,
refuses a non-candidate stack and writes one evidence record. `cells`, `bindings` and
`remote_bindings` gain one keyword, `owner`, that defaults to 3118. Existing callers keep
their behavior. Every cell runs the default shape on the catalog image (`rocky-kdive-ready-10`
locally, the enterprise representative remotely). Each cell binds the longterm fixture's
kernel inputs as its setup identity, the same way #3118 bound them for its non-booting
insufficient-disk cell. On a remote cell, `observe_host` records the provider host.

The in-flight window for the worker and Postgres faults is the provision's first-boot wait
(lost-response replays while the job is running). That window
opens when the owned domain is active and the job is `running` at attempt 1. It closes when
the job leaves `running`. `in_flight` polls libvirt and the job row every 0.5 s and returns the
running claim (worker id, attempt, lease deadline). Locally the window is the readiness-marker
wait. Remotely it is the guest-agent wait. If the job leaves `running` before the window is
seen, the cell fails with `fault point not reached (incomplete)`. It is never a pass.

At the in-flight point, the carrier reads the domain XML and sets `Held.overlays` and
`Held.disks`. `prove_held` and `release_and_verify` then check the real overlay. Without this,
they would pass an empty list.

The carrier checks these preconditions before `allocations.request`. The fixture is checked
there too; the image is acquired on the held Allocation, and a failure there is released by
`on_allocation`'s cleanup attempt. Each unmet one blocks the cell:

- at least two running fixed worker slots (`KDIVE_WORKER_COUNT=2`);
- no installed authority (`run_identity` reports no `authority` role), and a provision job
  payload with no `authority_system_v1` marker, which is re-checked after enqueue. This pins
  the direct path D1 selects.
- the longterm fixture and the staged image;
- for the remote cells, `REMOTE_PROVIDER_SSH`;
- `sudo -n /usr/bin/python3` with no prompt (probed with `sudo -n -l /usr/bin/python3`; the hold
  runs that interpreter with `-c`);
- the fixed workers' journal is readable (`systemd-journal` group);
- for backing-service, exactly one container for the `postgres` service of the stack's compose project
  (`name: kdive`);

Every wait has a bound:

- the in-flight window: one boot window;
- reclaim: the lease remainder plus 120 s, and at most 8 minutes;
- terminal job: 3 attempts times one boot window, plus the lease (5 minutes), plus 120 s;
- stale journal: one first-boot window plus 60 s;
- readyz: 120 s.

An expired bound records the cell as an incomplete failure.

### worker-interruption

1. Provision with the default shape. Wait for `in_flight`.
2. Pause the holder with `set_exact_worker_hold(claim, "stop")`.
3. `wait_for_reclaimed_job` must observe a different worker claim a higher attempt after the
   lease lapses. The deadline is the lease remainder plus 120 s, and at most 8 minutes. The
   Sampler runs every 15 s while waiting.
4. Wait until the job is terminal. The cell requires `succeeded` at the replacement's attempt
   by the replacement worker, and the System `ready`.
5. Resume with `"continue"`. Wait at most one first-boot window plus 60 s (900 s locally, 180 s remotely) for
   the stale invocation's journal to show one of two lines for this job:
   - `completed but was reclaimed; result dropped`, or
   - a provision failure line.
   Record which line appeared. Then require each of the following, recorded separately so a
   failure names the stale effect:
   - the job row is unchanged (`succeeded`, same attempt, same worker);
   - the System is still `ready`;
   - the owned domain is still active with the same UUID;
   - the overlay recorded at the in-flight point still exists.
6. Prove `worker-interruption` with the original and replacement claims, the paused invocation id,
   the stale outcome and the final job row. Prove `no-duplicates`: one System under the
   Allocation, one added domain named `kdive-<system_id>`, and one overlay equal to the one
   observed at the in-flight point. Prove `protected-state` from the samples.
7. Release through `Held.release` (`release_and_verify`). That proves `cleanup`: the System,
   domain and overlay are gone and capacity is back to its value before the cell.

A `finally` always resumes a still-paused holder before the cleanup attempt.

### lost-response

1. Generate a fresh idempotency key `q3117-<uuid4>`.
2. Call `Held.provision(profile, idempotency_key=key)`. That sets `system_id` and the
   Sampler.
3. Require the `idempotency_keys` row for (key, `systems.provision`) to exist, using an
   evidence-only database read.
4. Drop the returned envelope from all later use, together with the `system_id` that
   `Held.provision` recorded. From here on, the carrier knows the job and System only from the
   replay and the stored row. That is the lost response: the server committed and the caller
   holds no answer (D2).
5. Retry from a new `LiveStackClient` session with the same key, while the job is still
   `running`. Retry again after the job is terminal.
6. Require all of the following:
   - each replay's job id equals the stored `envelope.job.id`;
   - each replay's `system_id` equals the stored `envelope.system_id`;
   - the replay's `system_id` equals the one `Held.provision` returned;
   - exactly one `provision` job references a System of the Allocation;
   - exactly one System exists under the Allocation;
   - the job is `succeeded` and the System `ready`.
7. Read the domain XML into `Held.overlays` and `Held.disks`. Prove `lost-response` with the
   stored row, both replays and the job and System counts. Prove `no-duplicates`,
   `protected-state` and `cleanup` as in worker-interruption.

### backing-service

1. Before the cell, record:
   - the compose `postgres` container id;
   - each running worker unit's `InvocationID` and `MainPID`, from `systemctl show`.
2. Provision. Wait for `in_flight`.
3. Stop Postgres with fixed argv: `docker compose --project-directory <stack checkout> stop
   postgres`.
4. Hold the outage for 75 s, which is more than two heartbeat intervals. During the outage the
   carrier makes no MCP call. Libvirt observation of the owned domain continues.
5. Start Postgres with `docker compose ... start postgres`. Require the same container id.
6. Wait at most 120 s for every role's readyz to return ready.
7. Wait, within the terminal-job bound, for the job to be terminal. The documented resume path
   requires all of the following:
   - the job `succeeded` at an attempt above 1;
   - the System is `ready`;
   - exactly one domain is active.

   The carrier labels `resume_path` from the worker journal and the job row:
   - `requeued`: a `job <id> failed: infrastructure_failure` line exists for attempt 1;
   - `lease-reclaimed`: there is no failure line, and a heartbeat-failure line exists;
   - `unknown`: neither line exists, and the evidence is recorded.

   An attempt-1 success cannot pass. It means the outage never reached the in-flight attempt,
   so the cell records an incomplete failure.
8. Require every worker unit to keep its pre-fault `InvocationID` and `MainPID`. This shows the
   outage restarted no slot and wedged none. Server and reconciler restarts are not part of
   the claim. Their readyz in step 6 is the usability check.
9. Prove `backing-service` with the outage interval, container id, readiness timeline, attempt
   evidence and terminal job. Prove `no-duplicates`, `protected-state` and `cleanup` as above.
   The protected-state samples bracket the outage, because Postgres holds the allocation and
   capacity state and nothing can change it while it is stopped.

A `finally` always starts a stopped Postgres before any cleanup call.

### Coverage and runbook

- `scripts/coverage_campaign/obligations.toml` maps the three local cells to
  `tests/integration/test_orchestration_faults_live.py::test_local_orchestration_fault` and the
  three remote cells to `::test_remote_orchestration_fault`.
- The carrier's `main` writes input bindings with `owner=3117`.
- `docs/operating/runbooks/live-testing.md` gains a run section, and `remote-live-stack.md`
  gains the remote arm. Both name the preconditions above, the commands and the cleanup
  checks.

## Failure model

1. **Actors and deployments.**
   - The operator runs the live carriers on one exclusive x86_64 lab host. That host runs the
     managed host stack (`stack-services.sh`/demo-up, `KDIVE_WORKER_COUNT=2`) with the
     candidate deployed.
   - The remote cells add one separate x86_64 remote-libvirt provider host reached over the
     operator's SSH.
   - CI runs only the unit tests and the contract test.
2. **Invariants and assets at stake.**
   - The fault must be real and must hit the in-flight window.
   - A cell counts as passed only with every assertion proven on the candidate.
   - Each cell holds one owned System, domain and overlay. The Allocation stays granted and
     capacity stays charged until release.
   - The stack must stay usable: no wedged worker slot (SIGSTOP and SIGCONT only), Postgres
     restarted with its data volume, and every role ready afterwards.
   - The cell touches no domain, volume or container it does not own, except the shared
     `postgres` service, which needs the exclusive host grant.
3. **Accepted failure classes.**
   - A first boot too fast for the 0.5 s window poll. The cell records an incomplete failure
     and does not pass, and the cost is one rerun.
   - Lost-response proves only the committed-then-lost case, observed client-side (D2).
     Abort-before-commit is nondeterministic, and the admission transaction makes the System and
     the key atomic. Whether a disconnect cancels the handler is not tested here.
   - Samples are discrete, with no instantaneous guarantee between them. The same bound is
     accepted in #3118.
   - The kernel inputs are bound as setup identity and are not booted (D4).
   - A provision interrupted between overlay creation and overlay customization is not
     exercised. It is a follow-up candidate, not a fault point here.
4. **Covered elsewhere.**
   - Low-resource cells: #3118.
   - ppc64le: #2818.
   - Authority-path recovery and orphan/conflict construction: #3152 and #3185, plus the merged
     #2151 carriers.
   - Durable finalization and cross-replica guarantees: #2681.
   - Object-store outage on capture publication: no owner in this charter (see D3).

## Validation

These unit tests use realistic records and boundary fakes only. Each new test bites against a
controlled fault.

- `in_flight`:
  - accepts an active owned domain with the job at attempt 1;
  - keeps polling on attempt 0 or before the domain exists;
  - fails `incomplete` when the job is terminal first.
- Worker-interruption verdict. It refuses each of these:
  - a replacement with the same worker or attempt;
  - a non-succeeded job;
  - a changed job row after resume;
  - a System moved to `failed`;
  - a missing or torn-down domain;
  - a reclaimed overlay;
  - a stale journal with no matching line.
- Lost-response verdict. It refuses each of these:
  - a replay whose job id or `system_id` differs;
  - two provision jobs;
  - two Systems;
  - a stored row that is missing.
  The fixture row is built with `stored_admission_result`, so it has the real
  `envelope.job`/`envelope.system_id` shape.
- Backing-service verdict. It refuses each of these:
  - a changed container id;
  - a worker unit whose `InvocationID` or `MainPID` changed;
  - readiness that never returns;
  - a failed terminal job;
  - an attempt-1 success.
  It labels `requeued`, `lease-reclaimed` and `unknown` from realistic journal lines.
  The compose argv builder is tested to accept only `stop` and `start` of `postgres`.
- `cells(owner=3117)` resolves exactly the six cells.
  `tests/scripts/test_coverage_contract.py` requires the six node bindings.
- The `owner` default still selects exactly #3118's six cell ids, taken from
  `build_contract()`.
- First real run: the bounded live plan in the design-gate report, after an operator grant.

## Conditional stale-attempt fence fix

Operator decision, 2026-10-20: this fix is in scope only if a live worker-interruption cell
confirms that a stale, reclaimed provision attempt changes the System or its provider
resources. "Changes" means one of these:

- the System leaves `ready`;
- the domain is torn down;
- the overlay is reclaimed after the replacement attempt has committed.

If no cell confirms it, the code is left unchanged.

When it is confirmed, the fix guards each stale write with the job's attempt fence, under the
SYSTEM advisory lock. Before any of those writes, the handler re-reads `jobs.attempt` and
`worker_id`. The guarded writes are:

- `_record_system_failure` (`src/kdive/jobs/handlers/systems.py:321-334`);
- the post-commit teardown in `_execute_system_lifecycle_call`.

The provider's own reclaim on failure (`provisioning.py:383-391`, plus the remote equivalent)
takes a fence callback that is checked before it tears anything down. The exact seam is
designed against the observed failure and recorded in ADR 0794 if it has viable alternatives.

The fix needs a red-green unit test that drives a reclaimed attempt's late failure against a
`ready` System, plus the gauntlet code review. The failing cell is then rerun. Try a
handler-only fence first. Any of these is a new scope checkpoint:

- changing the attempt fence itself;
- adding a migration;
- changing the provider port's `provision` signature (`src/kdive/providers/ports/lifecycle.py`);
- changing any non-libvirt provider.

## Operator decisions (settled 2026-10-20: option (a) for each)

- **D1 execution path.** Chosen: the direct path on the managed host stack. Rejected:
  the authority lane, which waits on #3152 and #3185.
- **D2 lost-response transport.** Chosen: discard the received envelope after the commit
  is visible, then retry with the same key from a new session. Once the commit is visible, the
  server cannot tell this apart from a dropped connection (`provision.py:130-146`).
  Rejected: a raw streamable-HTTP client that abandons the connection without reading the
  response. That adds about 100 lines and observes nothing more at the server.
- **D3 backing service.** Chosen: stop Postgres during the first-boot wait. Rejected:
  stop the object store around `runs.install`. That alternative only shows a paused dequeue,
  not an in-flight loss.
- **D4 kernel.** Chosen: bind the kernel inputs only (#3118 precedent). Rejected: install
  and boot the fixture after recovery, which adds about 5 minutes per cell.
