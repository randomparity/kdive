# Domain concepts

KDIVE separates experiment history from leased VM capacity. The main relationships are:

```text
Capacity: Resource → Allocation → System
History:  Investigation → Run → DebugSession
                          └── binds to a System
```

These records live in Postgres. A record's state, the existence of a live guest, and the
retention of its artifacts are separate facts.

## Capacity

**Resource** describes a registered provider host and its capabilities. Production defaults
to local libvirt; remote libvirt is an operator-configured option. Select a host using its
advertised guest-architecture support and availability. See the [resource reference](reference/resources.md).

**Allocation** is a project's capacity claim. Admission checks permissions, quota, budget,
and capacity. A request may be granted immediately or queued; a queued request is not yet
usable capacity. A grant has its own lease window, shared by operations using that Allocation.
Read and renew it through the [allocation tools](reference/allocations.md), which document
the deadline, reference clock, and expiry behavior.

**System** is the VM target associated with an Allocation. Provisioning creates its record
and queues the provider work; a successful job leaves the System ready for use. The current
admission path permits one System per Allocation. Reusing that System for another kernel
experiment differs from provisioning another System: install and reprovision have their own
preconditions. See the [System reference](reference/systems.md).

## Experiment history

**Investigation** groups Runs toward a goal such as reproducing and fixing a bug. It belongs
to a project and records the creating principal; access follows project grants, rather than
being restricted to the creator. Its Runs can use different Systems, Allocations, and provider
kinds. Some Systems also name an Investigation directly, for Investigation-owned rootfs reuse
and cleanup. See the [Investigation reference](reference/investigations.md).

**Run** records an experiment attempt using a kernel build. Build externally and upload the
artifacts, or reuse a compatible, unexpired build from the same Investigation. A Run can be
created with a ready System or left unbound with a `target_kind`. `runs.bind` connects an
unbound Run to a suitable System of that kind; the System determines its Allocation.

A Run's `succeeded` state means its **build** is complete, not that install or boot succeeded.
Read `runs.get`'s step and readiness information together with the relevant job's outcome.
The [Run reference](reference/runs.md) owns these fields and binding/build-reuse constraints;
[async jobs](async-jobs.md) explains polling.

**DebugSession** records a debug attachment to a Run for one boot. After reboot or crash,
do not assume the old transport remains usable; inspect the session and establish a valid
attachment before continuing. See the [debug reference](reference/debug.md).

## Separate lifetimes

An Allocation's lease governs use of its capacity. Expiry and physical cleanup are separate:
reconciliation is periodic, and provider failures or safety gates can delay reclamation.
A released or expired allocation is not proof that its guest and provider data are gone.
Follow the returned state and recovery guidance; the [errors guide](errors.md) covers failed
Systems and the current limits of their cleanup path.

Closing an Investigation records the outcome and schedules artifact cleanup. Its close
contract also checks directly bound live Systems; it is not simply a label change or a
promise to preserve every build indefinitely. Experiment records, uploaded builds, rootfs
data, and VM snapshots have different retention rules. Consult the owning tool's contract
before closing an Investigation or relying on a retained artifact.

For implementation, read the [state definitions and transition table](../../src/kdive/domain/capacity/state.py).
The [architecture](../design/top-level-design.md) explains the service structure; ADRs record
the decisions and their later amendments.
