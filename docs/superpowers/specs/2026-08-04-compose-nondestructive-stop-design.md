# Compose non-destructive stop design

## Scope charter

- **Interaction:** interactive.
- **Scope identity:** the review follow-up for
  `review::.::holistic::high_level_elegance::install_process_topology_stale`.
- **Outcome:** expose the existing evidence-preserving Compose `down` operation as a supported,
  non-destructive recipe so an old worker can be stopped before a fence-protocol migration without
  deleting the database volume.
- **Completion criteria:** the recipe records terminal worker evidence before removal, stops the
  Compose graph, preserves named volumes, is documented in the stop-old-first upgrade sequence,
  and has a guard that distinguishes it from the destructive teardown recipe.
- **Provenance:** the user's instruction to fix every desloppify queue item properly; ADR-0533's
  stop-old-first requirement; the accepted worker-crash artifact-fences design's supported Compose
  stop operation; and the adversarial topology review that reproduced the missing public path.
- **Exclusions:** no new lifecycle state, no Docker API widening, no raw-Docker workflow, no change
  to `ComposeWorkerLifecycle.down`, and no production support claim for arbitrary Compose-derived
  deployments.
- **Surface:** `justfile`, `tests/compose/test_compose_lifecycle_recipe.py`,
  `docs/operating/docker-compose.md`, `deploy/compose/README.md`, `docs/operating/install.md`,
  `docs/operating/runbooks/build-use-recovery.md`, and the topology documentation guard.
- **Ambiguities:** none. The existing wrapper behavior and upgrade order are already settled.

## Problem

The lifecycle wrapper already implements `down(volumes=False)`: it records the exact managed
worker's terminal outcome, removes the worker, runs `docker compose down --remove-orphans`, and
preserves named volumes. The public `just compose-down` recipe always adds `--volumes`, however, so
operator guidance cannot truthfully name a supported non-destructive stop path. `compose-recreate-
worker` is not a substitute because it immediately starts a replacement before migration.

Documentation that merely says “use the wrapper's down action” creates a phantom workflow. The
missing recipe is the defect.

## Contract

Add `just compose-stop`. It invokes the same authority-scoped wrapper as the other lifecycle
recipes with action `down` and without `--volumes`.

The operation therefore has this sequence:

1. inspect the exact managed worker;
2. persist terminal evidence through the lifecycle-witness database authority;
3. remove that worker and its credential handoff;
4. stop and remove the remaining Compose services and network;
5. retain all named volumes, including Postgres data.

For the reference local stack, the worker-fence upgrade path is:

1. `just compose-stop`;
2. select the new image/configuration;
3. `just compose-up`.

The existing `compose-up` graph applies migrations before local role bootstrap, starts the
non-worker services, and only then lets the lifecycle wrapper register and start the current
worker. The bootstrap resets the fixed local development passwords and restores the intended
runtime-role memberships. This supplies stop old → migrate → reset local development roles →
start current worker without a raw Docker lifecycle command.

This three-command path is local-bootstrap-only and requires `KDIVE_LOCAL_ROLE_BOOTSTRAP=1` (the
default). Setting it to `0` disables that local database mutation; an externally provisioned
Compose-derived deployment must supply its own equivalent ordered provisioning gate outside this
reference workflow. This design does not prescribe that downstream workflow's commands, Secret
format, or credential mechanism.

`just compose-down` remains the destructive development teardown and continues to pass
`--volumes`. The two recipes must have distinct names and comments so preserving data is never
implicit.

## Testing

Extend `tests/compose/test_compose_lifecycle_recipe.py` to assert:

- `compose-stop` invokes `compose_worker_lifecycle down` without `--volumes`;
- `compose-down` still includes `--volumes`;
- both recipes provide the lifecycle-witness database authority;
- both `docs/operating/docker-compose.md` and `deploy/compose/README.md` list `compose-stop` among
  the supported recipes, distinguish its volume-preserving stop from destructive `compose-down`,
  show `compose-stop` → select image/configuration → `compose-up` for a local-bootstrap
  fence-protocol upgrade, and distinguish fixed-password reset from credential rotation;
- the install and recovery guidance uses the same local-only sequence, states that external
  provisioning is outside the reference workflow, and does not direct operators to raw worker
  lifecycle commands.

The existing `ComposeWorkerLifecycle.down` unit and live tests remain the behavioral proof that the
wrapper records evidence before Compose teardown and preserves volumes when its `volumes` argument
is false.

## Threat model

### Boundaries and actors

- A local operator invokes a repository recipe and controls its environment and selected image.
- Docker returns container metadata to the existing lifecycle gate.
- The gate writes termination evidence using the lifecycle-witness database credential.

No new boundary is added. The public recipe exposes an existing wrapper action without exposing the
Docker socket or database authority to the worker.

### Controls

- The recipe calls the existing wrapper rather than raw Docker. Its container-ID validation,
  evidence persistence, credential cleanup, and fail-before-destructive-action behavior remain the
  controls at the Docker/database boundary.
- The witness DSN uses the same scoped environment/default as existing lifecycle recipes.
- A textual recipe guard pins the absence of `--volumes`; a live wrapper test already proves the
  lifecycle ordering.
- Failure remains fail-closed: if evidence cannot be persisted, the wrapper retains the worker and
  does not run Compose teardown.

### Out of scope

Host-root Docker can bypass the wrapper and remains unsupported. This change does not turn the
reference dev/demo Compose stack into a production deployment or define credential rotation for an
arbitrary downstream Compose topology.

## Alternatives rejected

- **Document `python -m kdive.processes.lifecycle.compose_worker_lifecycle down`.** This exposes
  an internal module invocation while the repository declares `just` recipes as the public
  workflow.
- **Reuse `just compose-down`.** It deletes the Postgres volume and cannot support an existing-data
  migration.
- **Use `compose-recreate-worker`.** It starts the replacement immediately and violates
  stop-old-before-migration ordering.
- **Add another wrapper action.** The wrapper's existing non-volume `down` behavior is already the
  required operation; another action would duplicate it.

No new ADR is required: ADR-0533 and the accepted worker-crash artifact-fences design already decide
that Compose stop is an evidence-gated operation and that old workers stop before migration. This
change supplies the missing public entry point for that settled decision.
