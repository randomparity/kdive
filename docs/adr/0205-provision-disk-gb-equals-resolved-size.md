# ADR 0205 — provision disk_gb must equal the allocation's resolved size

- **Status:** Accepted
- **Date:** 2026-06-21
- **Deciders:** KDIVE maintainers

## Context

`systems.provision` reconciles a submitted provisioning profile against the allocation's
persisted sizing snapshot via `reconcile_profile_sizing`
(`src/kdive/profiles/provisioning.py`). The snapshot (`vcpu` / `memory_mb` / `disk_gb`) is the
at-grant authority (ADR-0024): a profile may **omit** a sizing field (it is filled from the
snapshot) or **restate** it, but a restated value must equal the snapshot. A conflicting
restatement is rejected with a `configuration_error`
(`provisioning profile disk_gb=N conflicts with the allocation's resolved size M`) so the
admitted size and the booted size can never diverge.

The `vcpu` / `memory_mb` arms of this rule were exercised by the existing unit tests
(`tests/profiles/test_provisioning.py`), and the remote spine had already learned to keep its
allocate request and provision profile in sync through one constant
(`REMOTE_ALLOCATION_DISK_GB`, asserted by `test_remote_provision_profile_validates`). The
**local** spine did not: its `allocations.request` booked `disk_gb=10`
(`tests/integration/test_live_stack.py:221`) while its `_provision_profile()` declared
`disk_gb=20` (`:122`). Those two literals were independent and had drifted, so the local spine —
had its gated path run end to end — would fail its own `provision` phase with exactly the
conflict above. The #115 live verification (2026-06-21) surfaced this drift; it is filed as #656.

This is a stale-fixture defect, not a validation defect: the equality rule is the intended
invariant, and the fixtures are what violated it.

## Decision

Keep the **strict equality** rule in `reconcile_profile_sizing` unchanged: a restated `disk_gb`
(and `vcpu` / `memory_mb`) must equal the allocation's resolved size, and any other value —
larger or smaller — is a `configuration_error`. The disk a profile provisions is the disk the
allocation booked; there is no "request ≤ booked" lane.

Fix the drift at its source. Add one constant `LOCAL_ALLOCATION_DISK_GB` to the shared spine
module (`tests/integration/live_stack/spine.py`, alongside the existing
`REMOTE_ALLOCATION_DISK_GB`) and read it from **both** local spine sites — the
`allocations.request` body and `_provision_profile()` — so the two can no longer drift apart.
This mirrors the remote spine exactly.

Pin the invariant with a non-gated (CI-runnable) unit test in `test_live_stack.py`,
`test_provision_profile_disk_gb_equals_allocation_request`, covering **both** cases the
acceptance criteria name:

- **Passing:** reconciling the real `_provision_profile()` against an `AllocationSizing` built
  from `LOCAL_ALLOCATION_DISK_GB` succeeds and yields that disk — proving the profile factory
  agrees with the allocate request.
- **Conflicting:** a profile that over-asks (`disk_gb = LOCAL_ALLOCATION_DISK_GB + 1`) is
  rejected as `CONFIGURATION_ERROR` on the `disk_gb` field — proving the rule is equality, not
  `≤`.

The factory reads the kernel-tree / guest-image paths from the environment (real values gate
the live spine through `_spine_preflight`); the unit test stubs them via `monkeypatch` so it
stays CI-runnable without touching the factory's fail-fast contract.

No `src` change, no schema/migration change, no MCP-surface change.

## Consequences

- The local spine's allocate request and provision profile read one constant, so a future edit
  to either site cannot silently re-introduce the #656 self-conflict; the non-gated test fails
  in CI if they drift.
- The equality invariant now has an explicit local-spine regression test in addition to the
  unit-level `reconcile_profile_sizing` tests, and the same invariant is documented once here as
  the convergence anchor for both spines.
- The rule remains strict equality. An operator who wants a different guest disk than the booked
  allocation changes the allocation's `disk_gb` (the booked size), not the profile.

## Considered & rejected

- **Relax the check to `≤` (a profile may request less than the booked size).** The issue's
  alternative. Rejected: the allocation's resolved size is the booked-and-billed disk; letting
  the profile silently provision a smaller disk would make the admitted size and the booted size
  diverge, which is the exact divergence ADR-0024's snapshot authority exists to prevent. A
  smaller guest disk is expressed by booking a smaller allocation, not by under-asking at
  provision.
- **Drop the `disk_gb` from `_provision_profile()` entirely (let it fill from the snapshot).**
  Valid — an omitted field is filled from the snapshot — but it removes the explicit restatement
  the spine fixture documents and would make the profile silently track any future request
  change without a test catching a true mismatch. Keeping an explicit restatement bound to the
  same constant keeps the fixture self-documenting and the equality genuinely asserted.
- **Two independent literals (the status quo).** The defect itself: independent `10` and `20`
  drifted. Rejected — one constant per spine is the fix.
- **A single shared constant for both spines.** The remote and local spines are distinct
  fixtures with independently chosen sizes; coupling them under one constant would make a
  remote-only size change perturb the local spine. Rejected for a parallel `LOCAL_`/`REMOTE_`
  pair, matching the existing per-spine factories.
