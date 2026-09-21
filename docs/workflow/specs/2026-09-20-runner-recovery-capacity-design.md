# Runner recovery capacity (#2563)

## Problem

The runner inherits eight 32-GiB recovery reservations (256 GiB), exceeding
the 208365330432 available bytes measured in issue #2563.

## Scope

Set `live_vm_host_external_boot_concurrent_activations: 6` in
`deploy/ansible/inventory/group_vars/live_vm_runners.yml` (192 GiB).
Clarify in `local_worker_host/defaults/main.yml` that recovery admission is
independent of the fixed eight worker slots. Preserve the generic default of
eight and the 32-GiB reservation. Inventory remains the deployment-specific
owner; the existing capacity assertion consumes it. No ownership transition,
caller migration, new interface, or ADR is needed.

The campaign approved these exclusions on 2026-09-20: filesystem sizing
(runner operator), reservation derivation (future capacity-sizing work),
Python headers (#2561, fixed by #2611), and worker topology (ADR-0574).

### Failure model

- Actors and deployments: operators reprovisioning the declared KVM runner.
- Invariants and assets: a measured capacity floor before worker release;
  generic defaults and eight isolated worker slots retain their contracts.
- Accepted failure classes: later disk exhaustion still fails the existing
  capacity assertion; the issue's measurement is a regression fixture, not
  a promise of future free space.
- Covered elsewhere: filesystem growth by the runner operator; reservation
  sizing by future capacity work; worker topology by ADR-0574.

## Success

The effective runner requirement is 192 GiB and fits the issue's measurement.
The generic requirement remains 256 GiB; the runner still has eight slots.
The defaults explain the two independent counts.

## Validation

- Mode: focused-test. Add
  `test_runner_external_boot_capacity_fits_measured_free_space` to
  `tests/deploy/test_live_worker_provisioning.py`, resolving defaults plus
  runner inventory and checking the admitted bound and byte requirement.
  Expected red: inherited eight exceeds the measured capacity.
  Green command: `uv run python -m pytest tests/deploy/test_live_worker_provisioning.py::test_runner_external_boot_capacity_fits_measured_free_space -q`.
  Assert preserved generic values and slot count in the same configuration proof.
- Mode: task-test-not-applicable. Defaults explanation and this spec are
  human-facing prose without an executable consumer; review their meaning.
- Run the existing provisioning file, `just lint`, `just type`,
  `just test-ansible`, staged hooks, and final `just ci`.
  Exercise the existing capacity assertion locally using runner inventory,
  measured free bytes, and a below-threshold failure. Full runner provisioning
  is outside this correction; available disposable hosts are not the measured runner.
