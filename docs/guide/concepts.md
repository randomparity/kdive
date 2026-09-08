# Domain concepts

KDIVE separates experiment history from leased VM capacity. Six domain objects connect a
kernel experiment to its resources; their records live in Postgres and bulk artifacts live
in the object store.

## The six objects

```text
Investigation ──< Run ──< DebugSession
                  │
                  └── System ── Allocation ── Resource
                      (after binding)
```

**Resource** is bookable capacity registered or discovered by a provider. The implemented
production providers use local and remote libvirt hosts. Resource descriptions expose
capabilities and availability; use them to select a host that can run your target architecture.

**Allocation** is a project's capacity claim for a lease window. Admission checks permissions,
quota, budget, and capacity. A successful request returns `granted`, or `requested` when queued;
use `allocations.wait` to follow a queued request. The lease is per allocation: `lease_expiry`
is an absolute ISO-8601 UTC deadline measured against `server_time`. Renew with
`allocations.renew` before expiry; after expiry the reconciler reclaims the allocation and its
Systems. See the [allocation reference](reference/allocations.md) for recovery and constraints.

**System** is a provisioned VM associated with an Allocation. `systems.provision` creates its
row in `provisioning` and queues provider work; successful provisioning makes it `ready`.
Installing another kernel reuses the System. Reprovisioning cycles the same System through
`reprovisioning` back to `ready` or `failed`. Its live infrastructure is bounded by its
Allocation's lifetime. See the [System reference](reference/systems.md).

**Investigation** groups Runs toward a goal such as reproducing and fixing a bug. It is scoped
to a principal and project and may span Systems, Allocations, and provider kinds. Closing an
Investigation does not cascade to its Runs. See the [Investigation reference](reference/investigations.md).

**Run** records one kernel-build attempt within an Investigation. Build the kernel externally
and upload it, or reuse a compatible build from the same Investigation. Create a Run bound
to a ready System, or omit `system_id` and supply `target_kind` to create it unbound; later use
`runs.bind` to select the System. Binding determines the Run's Allocation.

A Run's `succeeded` state means its **build** succeeded. It does not establish that install or
boot succeeded: inspect `runs.get`'s `data.steps` and `data.boot_readiness`, and follow the
returned job's outcome. See the [Run reference](reference/runs.md) and [async jobs](async-jobs.md).

**DebugSession** records a debug attachment to a Run and its current boot. Reboot or crash
invalidates live transport state; inspect the session and establish a valid attachment before
continuing. See the [debug reference](reference/debug.md) for supported transports and operations.

## Lifecycle ordering

A System's live resources cannot outlast its Allocation. The reconciler repairs orphaned
infrastructure and affected Runs when leases expire or Systems are torn down. Teardown does
not erase the Investigation's experiment history. Artifact retention and allocation leases
are separate lifetimes; follow the deadlines and recovery actions returned by the tools.

For implementation, the [state definitions and transition table](../../src/kdive/domain/capacity/state.py)
are the current state-machine contract. The [architecture](../design/top-level-design.md)
explains how the service enforces it; ADRs record the decisions and their later amendments.
