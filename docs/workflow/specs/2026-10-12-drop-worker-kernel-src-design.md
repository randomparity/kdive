# Drop the worker kernel-source requirement (#3193)

## Problem

`scripts/live-stack/worker-lifecycle.sh start` refuses to start the fixed workers unless
`KDIVE_KERNEL_SRC` names an existing absolute directory that every slot account can read. It then
sends that path to the root-owned lifecycle witness as the required `source_root` start setting,
which the witness writes into each slot's `worker.env` as `KDIVE_KERNEL_SRC` and
`KDIVE_WORKER_SOURCE_ROOT`. No worker code reads either variable: ADR-0316 removed the server-build
lane, and the `KERNEL_SRC` setting in `src/kdive/config/core_settings.py` has no reader. A
freshly prepared host with no kernel checkout therefore cannot run
`examples/local-libvirt/demo-up.sh`, even when it only provisions catalog images.

`KDIVE_KERNEL_SRC` itself stays meaningful. The live proofs, `scripts/live-vm/preflight-env.sh`,
and `scripts/operations/live_debug_build.py` read it in their own process as the built kernel
fixture tree.

## Design

Remove the worker-side plumbing end to end. Do not make it optional.

1. **Launcher** (`scripts/live-stack/worker-lifecycle.sh`). Delete the absolute-directory check
   and the `rx` access check on `KDIVE_KERNEL_SRC` in `require_start_prerequisites`, the
   `KDIVE_SOURCE_ROOT` handoff, and the `source_root` key in the request it builds.
2. **Contract** (`systemd_worker_contract.py`). Delete `WorkerSettings.source_root` and its two
   validator registrations. `extra="forbid"` stays, so a request that still carries
   `source_root` is rejected as `invalid_request`.
3. **Witness** (`systemd_worker_state.py`). Stop writing `KDIVE_KERNEL_SRC` and
   `KDIVE_WORKER_SOURCE_ROOT` into `worker.env`.
4. **Gate** (`deploy/systemd/bin/kdive-live-worker-gate`). Drop both names from
   `_WORKER_ENV_NAMES`. A `worker.env` written before the upgrade may still carry them, and the
   gate then drops them instead of passing them to the worker.
5. **Configuration.** Delete the `KERNEL_SRC` registry setting. Catalogue `KDIVE_KERNEL_SRC` in
   `src/kdive/config/external_env.py` as a `test`-scope variable: the built kernel tree read by
   the live proofs and operator scripts, never by a worker. Delete the `KDIVE_SOURCE_ROOT` and
   `KDIVE_WORKER_SOURCE_ROOT` entries (no reference to either name remains, so the env-docs guard
   stays green), then regenerate `docs/guide/reference/config.md`.
6. **Host preparation** (`deploy/ansible/playbooks/local-libvirt-host.yml`). The task that makes
   kernel-tree ancestors traversable exists only to satisfy the removed `rx` check. Remove the
   kernel root from that task and drop `local_libvirt_host_kernel_source` and its resolved fact.
   Checkout traversal is unchanged. The task stops widening the operator's home to `o+x` on a
   tree's behalf.
7. **Demo** (`examples/local-libvirt/`).
   - Keep the `KDIVE_DEMO_WORKSPACE` fallback to `KDIVE_KERNEL_SRC`. A developer's kernel tree
     is still the natural agent workspace, and changing that default is a behaviour change
     nobody asked for.
   - With neither variable set, `demo-up.sh` keeps its actionable exit after the stack is up.
     Its message names `KDIVE_DEMO_WORKSPACE` as the thing to set, any directory will do, and a
     kernel tree is no longer implied.
   - Remove the `Kernel :` summary line. Under `set -u` it aborts once the workspace is set and
     `KDIVE_KERNEL_SRC` is not, and the stack no longer uses a kernel tree.
   - The README's prerequisite becomes the workspace directory. Rewrite the `KDIVE_KERNEL_SRC`
     table row to say where it is read.
8. **Comment.** Refresh the stale `scripts/live-stack/env.sh` comment, which still cites a
   removed `local_kernel_src` diagnostic.

## Rollout: the lifecycle protocol identity changes

`lifecycle_protocol_identity()` hashes `LifecycleRequest.model_json_schema()`. Removing
`source_root` changes that hash, and `LIFECYCLE_PROTOCOL_VERSION` stays `1`, as it did for the
#3086 schema change. No hash-neutral variant exists: an optional field changes the schema too. No
repository file pins the current hash. The only literal, in `test_systemd_worker_contract.py`,
is a historical inequality.

Consequences:

- A host whose `/opt/kdive-live-worker-lifecycle/.venv` predates this change fails closed:
  `start`, `stop`, `status`, `recover` and `compatibility` stop in `require_compatible_lifecycle`
  with `installed lifecycle protocol does not match this checkout; reprovision the runner` before
  any request is sent or slot is mutated (`diagnostics` skips the check and still runs).
  `deploy/ansible/roles/live_vm_host/tasks/verify.yml` fails the same comparison. Running workers
  keep running, but a skewed checkout cannot stop them.
- Deploy order: stop the stack on the current checkout (`examples/local-libvirt/demo-down.sh` or
  `scripts/live-stack/stack-down.sh`), then update the checkout, then reprovision with
  `just prepare-local-libvirt-host` (local-libvirt hosts) or the `live_vm_host` role /
  `deploy/systemd/install-live-worker-lifecycle.sh` (runners), then start.
- Recovery for an operator who switched the checkout with workers running: check out the
  previous commit, stop the stack, switch back, reprovision, start. Never `stack-down.sh --force`
  for this; it strands fences.
- Rollback mirrors the deploy order: stop, revert, reprovision, start. `git revert` alone strands
  the host in the other direction.
- The self-hosted native `live_vm` job runs only on `schedule` or `workflow_dispatch`
  (`.github/workflows/live.yml`) and never installs the lifecycle venv, so this PR's checks stay
  green and the job fails closed at its first run after merge until the runner is reprovisioned
  with the `live_vm_host` role. The PR body names that action for whoever merges.

## Failure model

1. **Actors and deployments.** A local operator running `demo-up.sh` or `worker-lifecycle.sh`
   on a local-libvirt host. The self-hosted KVM runner (native `live_vm` job). The hosted TCG
   job, which installs the contract on each run. Root runs the lifecycle witness and the gate.
2. **Invariants and assets.**
   - The root-owned control request stays allowlist-only, and the change only narrows it.
   - A protocol skew fails closed before any slot mutation.
   - A worker still receives every variable it reads. Removing two names it never reads cannot
     drop a consumed one.
3. **Accepted failure classes.**
   - The native `live_vm` job is red from its first post-merge run until its runner is
     reprovisioned; the action and its owner (the merger) are stated above and in the PR body. It
     is the existing skew guard working as designed.
   - A `worker.env` written by a pre-upgrade witness keeps the two names until that slot is next
     prepared. The gate drops them, and no reader exists.
   - An operator who exported `KDIVE_KERNEL_SRC` for the workers sees no change in behaviour,
     because nothing read it.
4. **Covered elsewhere.**
   - The live proofs that need a kernel tree keep their own preflight
     (`scripts/live-vm/preflight-env.sh`, `tests/integration/test_live_stack.py`). That is an
     approved exclusion.
   - The `live.yml` kernel-tree fetches stay, also an approved exclusion.

## Threat model

- **Boundaries.** The launcher-to-witness request over the root-owned lifecycle socket. The
  witness-to-`worker.env` file. The gate's pass-through of environment variables into the
  worker. This change adds no boundary and narrows all three: one fewer caller-supplied path
  crosses into root and into the slot environment.
- **Actors.** A local operator in `kdive-live-control`, already trusted to send start requests.
  Slot accounts, which are untrusted with respect to each other.
- **Controls.**
  - The pydantic `extra="forbid"` rejection rejects a stale or crafted `source_root`.
  - Existing peer-credential and identity checks are unchanged.
  - The gate's fixed allowlist governs pass-through.
- **Out of scope.** Other allowlisted settings, which are unchanged.

## Considered and rejected

- **Make `source_root` optional.** This is judgment, not measurement. It changes the schema hash
  just as removal does, so it buys no rollout safety, and it keeps a request field and two
  environment names that nothing reads.
- **Add a preflight that only names the fix.** This is judgment. It keeps a requirement that
  nothing uses, which is the defect itself.
- **Default `KDIVE_DEMO_WORKSPACE` to a new directory.** This is judgment. It is a behaviour change
  outside the issue. Hosts without a tree already set the variable, as `scripts/host_install_proof.py`
  does.

## Follow-ups outside this surface

- `scripts/host_install_proof.py` still fetches `~/src/linux` citing the retired README
  prerequisite and traversal grant; dropping that fetch would let it exercise the no-tree path.
- `.github/workflows/live.yml` comments and `tests/scripts/test_live_workflow_shape.py` docstrings
  still say the worker captures `KDIVE_KERNEL_SRC` (the fetches themselves are excluded).

## Success

- `worker-lifecycle.sh start` builds and sends a valid request with `KDIVE_KERNEL_SRC` unset.
- A start request carrying `source_root` is rejected.
- `worker.env` and the gate's worker environment carry neither `KDIVE_KERNEL_SRC` nor
  `KDIVE_WORKER_SOURCE_ROOT`.
- `KERNEL_SRC` is gone from the registry, and `KDIVE_KERNEL_SRC` is catalogued as a
  live-proof/script variable. `just config-docs-check` and `just env-docs-check` pass.
- Host preparation no longer touches a kernel tree's ancestors.
- Live, on a lab x86_64 guest with no `~/src/linux` and `KDIVE_KERNEL_SRC` unset:
  `just prepare-local-libvirt-host` succeeds, then `demo-up.sh` (with only
  `KDIVE_DEMO_WORKSPACE` set to an empty directory) brings the stack to ready, and a catalog-image
  provision succeeds. A lifecycle venv provisioned at the base commit is shown to fail closed
  against this candidate until reprovisioned, or the reason it cannot be shown there is recorded.

The implementation plan is `docs/workflow/plans/2026-10-12-drop-worker-kernel-src.md`.
