# 0514 — The worker is a StatefulSet whose scratch volumes are per-replica

## Status

Accepted (2026-07-30)

## Context

Related: [ADR-0088](0088-deployment-packaging.md) (the chart this changes),
[ADR-0018](0018-job-queue-worker-execution.md) (the queue the workers pull from),
[ADR-0134](0134-chart-upgrade-config-drift.md) (the `checksum/config` pod annotation the worker
keeps), [ADR-0090](0090-opentelemetry-adoption-service-health.md) §5 (the aux port a Service
must not front), [ADR-0365](0365-helm-chart-version-independent-of-appversion.md) (the chart
version track).

The Helm chart exposed `worker.replicas` as a tunable and could not honour it.

`templates/pvc-worker.yaml` declared two release-scoped `ReadWriteOnce` PersistentVolumeClaims,
`<fullname>-build` and `<fullname>-install`. `templates/deployment-worker.yaml` mounted those two
claims **by name** into the worker Deployment's pod template, whose `replicas` came from
`.Values.worker.replicas`. A pod-template `persistentVolumeClaim` volume names exactly one claim
for every replica, so `worker.replicas: 2` had two outcomes and no way to choose between them:

- Replicas placed on **different nodes** — the second pod stays `Pending` with a `Multi-Attach`
  error. `ReadWriteOnce` is mountable read-write by a single *node*, by definition.
- Replicas placed on the **same node** — both start and share `/var/lib/kdive/build` and
  `/var/lib/kdive/install` for real build and install I/O.

Which one you got depended on the scheduler. That is why the existing render gate never caught
it: `helm template` renders one pod template regardless of replica count, and a single-node test
cluster lands in the second case and looks healthy. The defect was only reachable on a real
multi-node cluster, and only sometimes.

The failure was worth fixing rather than documenting away, because **parallel workers are a
designed shape of this system, not an aspiration**. `jobs/queue.py` claims work with `FOR UPDATE
SKIP LOCKED` specifically so "parallel workers claim disjoint rows without blocking"; job
deadlines are computed from the database's `now()` so "no worker clocks need to agree", a
constraint that only exists because more than one worker is expected; and `accepted_lanes` gives
each worker an explicit dispatch boundary so provider- or pool-specific workers do not acquire
work they cannot execute — a feature with no meaning at one worker. The engine supported
concurrency that the only supported deployment surface could not express.

The two volumes are **scratch for the deployments this chart produces**, which is what makes a
fix cheap. `KDIVE_BUILD_WORKSPACE` (`/var/lib/kdive/build`) is the worker's build workspace: a
warm kernel tree plus uuid-scoped per-build directories (`kdive-build-<uuid>`); the one
persistent artifact under it, a built rootfs qcow2, is uploaded to the object store as it is
produced. `KDIVE_INSTALL_STAGING` (`/var/lib/kdive/install`) is staging for install artifacts,
which on the remote path are pushed into the guest and not read again. Neither is a state of
record — that is Postgres — and no durable artifact lives only there.

That qualifier is load-bearing, so state the exception outright: on the **local-libvirt** path
the staged `kernel`/`initrd` are *not* scratch. `providers/local_libvirt/lifecycle/install.py`
writes them under the staging root and embeds their absolute paths into the domain XML as the
direct-boot `<kernel>`/`<initrd>`, so they are durable for the System's lifetime and deleting
them un-boots that System until a reinstall. This chart cannot reach that path: it sets
`KDIVE_LOCAL_LIBVIRT_ENABLED: "false"` (ADR-0127) and mounts neither a libvirt socket nor
`/dev/kvm`, so a worker pod has no local hypervisor to install into. An operator who wires one
up anyway inherits the caveat, and the values file and README say so.

## Decision

The worker is a **StatefulSet**. `build` and `install` come from `volumeClaimTemplates`, so
Kubernetes instantiates one `ReadWriteOnce` claim per volume per ordinal
(`build-<release>-kdive-worker-0`, `-1`, …). No claim is named in the pod template, so no claim
is shared, and the pod's `volumeMounts` are unchanged: `build` and `install` still land on
`/var/lib/kdive/build` and `/var/lib/kdive/install`, now backed by that replica's own volume.
`templates/pvc-worker.yaml` is deleted; a standalone PVC is by construction shared by every
replica that names it, so keeping one would leave the defect reachable.

Four properties come with the kind, each chosen rather than defaulted:

- **`serviceName` points at a new headless Service** (`templates/service-worker.yaml`), which
  declares `clusterIP: None` and **no ports**. A StatefulSet requires a governing Service and
  this one gives each replica stable per-pod DNS. It publishes nothing: nothing calls into a
  worker, and declaring the aux port would put the unauthenticated `/livez` `/readyz` `/metrics`
  listener behind a named Service endpoint, which ADR-0090 §5 makes the network boundary's job.
  Kubernetes permits a port-less Service only when it is headless, which this is.
- **`podManagementPolicy: Parallel`.** Workers are interchangeable and claim disjoint rows; no
  ordinal owns a role or waits on another. The default `OrderedReady` would serialize every
  scale and rollout for an ordering guarantee nothing needs.
- **`persistentVolumeClaimRetentionPolicy: {whenScaled: Delete, whenDeleted: Delete}`.** The
  volumes are scratch. Retaining them would strand a full pair of PVCs per removed ordinal, and
  a later scale-up would repopulate them from source and the object store anyway. This is the
  one place the chart destroys storage on the operator's behalf, so `Chart.yaml` declares
  `kubeVersion: ">=1.27.0-0"`: the field is gated behind `StatefulSetAutoDeletePVC`, which is
  off by default before 1.27, and an older API server prunes it silently — the chart would then
  promise to release those claims and leak them instead. Failing the install beats misleading.
- **`volumeClaimTemplates` metadata carries `name` and nothing else.** The field is immutable
  after creation — the API server rejects an update to any StatefulSet field outside `replicas`,
  `template`, `updateStrategy`, `minReadySeconds`, `ordinals` and the retention policy. The
  chart's shared `kdive.labels` embeds `helm.sh/chart`, which carries the chart version, so
  labelling the templates would make **every chart version bump** fail `helm upgrade` with an
  immutable-field error.

`worker.replicas` defaults to **2**. A default of 1 means no stock deployment ever exercises the
concurrent-claim path the queue was built for, so the first deployment to scale out is also the
first to test it.

Chart version goes to `0.5.0`. This is a breaking chart change with a manual migration, and the
chart version is the only signal an operator has (ADR-0365 keeps it on its own track from
`appVersion`).

## Consequences

**Existing releases need a drain before the upgrade, and lose the old volumes.** A StatefulSet
adopts only claims named `<template>-<statefulset>-<ordinal>`, so the release-scoped
`<fullname>-build` and `<fullname>-install` claims are not carried forward. Helm 3 converges the
rest on its own — it deletes resources the new chart no longer renders, matching on
name/namespace/kind, so the old Deployment (a different kind under the same name) and the two
PVCs are removed — but it creates the new resources before deleting the old ones, so a bare
upgrade runs both workloads concurrently for that window, some pods mid-build against volumes
about to be deleted. Scaling the Deployment to zero first makes the transition deterministic.
The chart README carries the procedure. Because the volumes are scratch, discarding them costs a
rebuild, not data.

**Storage requests multiply by replica count.** `worker.persistence.build.size` and
`.install.size` are now per replica, so the shipped default asks for 2 × (10Gi + 5Gi) = 30Gi
instead of 15Gi. An operator on a tight quota sets `worker.replicas: 1` or lowers the sizes.

**Changing a persistence size is no longer an upgrade.** `volumeClaimTemplates` is immutable, so
resizing needs the same delete-and-recreate the migration describes. This trades a capability
the chart never actually delivered (the old PVCs' `resources.requests` were equally unable to
grow in place on most StorageClasses) for one that now fails loudly at upgrade time instead of
silently.

**The default install now needs a StorageClass that can bind two claims per replica.** Any
default `ReadWriteOnce` StorageClass does; this asks for nothing exotic, which was the point.

**#1551 becomes reachable.** The live-testing runbook's Reuse arm claims to exercise the
per-`(investigation, checksum)` fetch lock but cannot, because the live stack starts exactly one
worker. That lock has no live coverage today. A chart that can express more than one worker is
the precondition for giving it some.

## Considered & rejected

- **`ReadWriteMany` PVCs, keeping the Deployment.** The smallest diff: change two access modes
  and nothing else. Rejected because RWX is not a property of the PVC, it is a property of the
  StorageClass — it requires NFS, CephFS, Azure Files, or similar. None of the common default
  StorageClasses (`hostpath`, `local-path`, the in-tree cloud block-storage provisioners) offer
  it, so the chart's default install would fail to bind on most clusters. Trading "breaks when
  you scale out" for "breaks on install unless you have shared storage" is not an improvement.
  It is also the wrong shape: sharing one build tree between concurrent `make` runs is a
  correctness question we would then have to answer, and per-replica volumes mean we never have
  to ask it.

- **Per-replica PVCs hand-rolled inside a Deployment** — render `worker.replicas` PVCs from a
  `range` and select one per pod. Rejected because a Deployment's pod template is one template:
  there is no per-pod substitution to select a claim with, so this needs either N single-replica
  Deployments or an init container that picks a claim at runtime. Both reimplement, worse, what
  `volumeClaimTemplates` is. The stable-identity-with-own-storage workload is the case
  StatefulSet exists for.

- **Leave `worker.replicas` at 1 and document that scaling is unsupported.** Rejected: it
  contradicts the engine. `SKIP LOCKED`, the database-clock rationale, and `accepted_lanes` are
  all multi-worker machinery already in the tree, and the chart was the only thing preventing
  their use. Deleting the knob instead would have been more honest than leaving it broken, but
  less useful than making it work.

- **Retain the per-replica PVCs on scale-down (`whenScaled: Retain`).** The Kubernetes default,
  and the safe choice for durable data. Rejected here because the data is not durable by design:
  the build workspace and install staging are reconstructible from Postgres and the object
  store. Retaining would accumulate abandoned claims on every scale event with nothing to show
  for them, and a scaled-back-up ordinal would reattach a stale tree rather than a clean one.
