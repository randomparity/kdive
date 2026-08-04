# Helm staged worker-fence upgrade design

## Scope charter

- **Interaction:** interactive.
- **Scope identity:** the final operational-safety review of
  `review::.::holistic::high_level_elegance::install_process_topology_stale`.
- **Outcome:** make the documented Kubernetes worker-fence upgrade ordering executable with the
  shipped Helm chart instead of relying on simultaneous workload reconciliation.
- **Completion criteria:** operators can render all four KDIVE workloads at zero, run migration,
  rotate credentials, start and verify only the lifecycle witness, and then restore workers; the
  chart prevents more than one witness replica; render and documentation guards pin the stages.
- **Provenance:** the user's instruction to fix desloppify items properly; ADR-0533's
  stop-old-before-migration protocol; ADR-0536's singleton witness boundary; and the adversarial
  review showing that a normal Helm upgrade restores witness and worker concurrently.
- **Exclusions:** no automatic credential rotation, no new Kubernetes controller, no change to
  worker finalizer evidence, no rolling upgrade claim for stop-old-first migrations, and no
  production contract for Compose-derived deployments.
- **Surface:** Helm values/template/render tests, the Kubernetes deployment runbook, the chart
  README, the install/recovery summaries, and the topology documentation guard.
- **Ambiguities:** none. Existing ADRs settle the required order and singleton authority.

## Problem

The chart hardcodes one lifecycle-witness replica while worker replicas are configurable. A normal
Helm upgrade applies both resources in one release reconciliation. External-backend migration hooks
run before that reconciliation; bundled-backend migration hooks run after it. Neither path gives an
operator a supported pause after migration and credential rotation in which only the witness starts
and becomes ready. Manual pre-upgrade scaling is also overwritten by the chart's hardcoded desired
state.

The current runbook therefore promises an ordering the shipped chart cannot enforce.

## Chart contract

Add:

```yaml
lifecycleWitness:
  enabled: true
```

The value accepts only a boolean:

- `false` intentionally holds the witness stopped during a staged maintenance window;
- `true` runs the singleton authority in ordinary operation;
- every other value fails template rendering with an actionable message.

The witness Deployment renders zero or one replica from this value. Default output remains
unchanged at one replica, so ordinary installs and existing values files retain current behavior.
A boolean expresses the only two valid singleton states without Helm's numeric coercion paths: a
JSON fraction can underflow to numeric zero before a template sees it, so a numeric `0|1` value
cannot provide the promised strict input boundary.

## Supported staged upgrade

The canonical procedure lives in `docs/operating/runbooks/kubernetes-deploy.md`:

1. Capture the release's current override values and the live desired `server`, `worker`, and
   `reconciler` replica counts before scaling anything. Persist those three counts separately; a
   `helm get values` file omits counts that came from chart defaults.
2. Keep the current witness and credentials healthy while scaling workers to zero. Wait until every
   worker Pod and its finalizer are gone.
3. Scale server and reconciler to zero, then scale the witness to zero. Verify all four KDIVE
   workloads have no running Pods and no KDIVE runtime database sessions remain.
4. Run the target Helm upgrade with `server.replicas=0`, `worker.replicas=0`,
   `reconciler.replicas=0`, and `lifecycleWitness.enabled=false`. The release's migration hook runs
   while no old or new application workload is active.
5. Complete the backend-specific credential stage while all four workloads remain at zero:
   - **External backends:** rotate each database role credential and update its referenced Secret
     after the migration succeeds.
   - **Bundled demo backends:** include the new `demoCredentials` values in the stage-4 all-zero
     upgrade. Its post-upgrade hook applies migration and resets the database role passwords before
     the stage succeeds; do not update only the generated Secrets afterward.
6. Run a hook-free Helm upgrade with only `lifecycleWitness.enabled=true`; keep server, worker, and
   reconciler at zero. Wait for witness rollout plus readiness. It runs the target image and reads
   the rotated credential because the Pod is newly created. `--no-hooks` is required: rerunning a
   stop-old migration while this database client is active would violate the maintenance boundary.
7. Run a final hook-free Helm upgrade with `lifecycleWitness.enabled=true` and explicit
   `server.replicas`, `worker.replicas`, and `reconciler.replicas` values from step 1. The
   already-ready witness remains available before workers are restored.
8. Verify worker incarnations and recovery-tool exposure before resuming queue processing.

The values file used for every Helm stage must be the freshly captured/edited release values, not
`--reuse-values`. Every stage also passes the three explicit captured replica counts or explicit
zeros and an explicit witness enabled state, so a new chart default cannot silently change scale.
The chart's existing prohibition on bare `--reuse-values` remains in force.

The install page, chart README, and build-use recovery runbook summarize the safety boundary and
link to this canonical procedure rather than publishing divergent command sequences.

## Failure and retry boundaries

After workers begin draining, never restore a pre-protocol worker or roll the release back to the
old image.

- If the all-zero hooked upgrade or migration fails, leave the manually scaled workloads at zero,
  correct the target-image migration/configuration, and retry the same all-zero hooked stage.
- If external credential rotation or witness readiness fails after migration, keep the three core
  workloads at zero, correct the target credentials/Secrets, and retry the hook-free witness-only
  stage.
- If the final hook-free restore fails, keep the ready witness running, scale any partially restored
  core workloads back to zero, correct the target release, and retry the hook-free final stage with
  the captured replica counts.

Forward recovery is the only supported path after the fence migration succeeds.

## Testing

Helm render tests must prove:

- the default `lifecycleWitness.enabled=true` renders one witness replica;
- `--set lifecycleWitness.enabled=false` renders zero while workers can independently render zero;
- numeric, decimal, string, and null inputs fail with the same boolean-value message;
- the staged-zero render leaves all four KDIVE workloads at zero.

Documentation guards must scope to the canonical staged-upgrade section and require all eight
stages, including explicit live replica capture/restoration, finalizer drain, the four-zero Helm
render, backend-specific post-migration credential handling, hook-free witness-only readiness,
hook-free final worker restore, and forward-only retry rules. They must also assert that summary
documents link to the canonical runbook.

## Threat model

### Boundaries and actors

- A Kubernetes operator controls Helm values, scaling commands, and referenced Secret material.
- Helm renders desired replica counts into the Kubernetes API.
- The lifecycle witness alone holds credential-broker and finalizer authority.
- Existing workers may be running an older fence protocol at the start of maintenance.

### Controls

- The witness enabled value is strictly boolean and maps only to zero or one replica, preserving
  ADR-0536's singleton authority.
- Old workers drain while the old witness credential remains valid; credential rotation occurs only
  after all old worker finalizers clear and migration completes.
- All application replicas render zero during the migration stage, preventing old/new database
  clients from racing stop-old-first migrations.
- A newly created witness Pod must pass its existing readiness probe before the final stage restores
  workers. Worker init remains fail-closed if the broker is absent or unusable.
- Render and text guards prevent a default or documentation change from collapsing the stages.

### Out of scope

An operator can still bypass the process with raw `kubectl`, force-delete finalizers, reuse old
credentials, or supply a malicious image. Those host/cluster-admin bypasses remain explicitly
unsupported and retain pins rather than creating recovery evidence.

## Alternatives rejected

- **Document simultaneous Helm reconciliation.** It cannot prove witness readiness before worker
  startup and is the defect being fixed.
- **Use only manual `kubectl scale`.** The next Helm reconciliation restores the hardcoded witness
  replica and makes release state disagree with the maintenance stage.
- **Expose a witness replica count.** A singleton has only running and stopped states. A numeric
  value exposes invalid counts and Helm can normalize a malformed JSON fraction to zero before
  template validation; a strict boolean represents the actual contract.
- **Allow multiple witness replicas.** The authority is designed and operated as a singleton; more
  replicas add broker and finalizer races without a decided leader-election contract.

No new ADR is required. ADR-0533 already decides the upgrade order, and ADR-0536 already decides
that the witness is a singleton separate workload. This change makes those accepted decisions
representable in the chart.
