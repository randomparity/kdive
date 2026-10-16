# Authority first-start convergence (#3233)

## Problem and authority

The role explicitly starts authority before flushing notified libvirt/socket changes.
Issue #3233 requires first start to consume converged inputs and an ordering regression.
Operator approval on 2026-10-16 covers moving the flush before explicit start,
retaining stopped-service recovery, handler order and post-convergence readiness.
Scope token q3233-73f26dce; campaign 2d21598e2016-b01982d7-4e2e-4af3-82b2-4b8aec96dc18.
Full-spec; S100 from issue/source assessment; no new interface or ADR decision.

## Design and success

Move the existing flush immediately before explicit start in provider_authority_host.
Preserve libvirt-before-authority handler declaration order and explicit state: started:
an unchanged stopped installation receives no notifications but must still start.
Keep libvirt verification and one-shot readiness after that explicit start.
Handler failure stops the play before explicit start or readiness.
The role retains ownership; callers, handlers, privileges and service inputs do not change.
Actual Ansible notification/flush execution, with harmless service boundary replacements,
must show fresh/changed convergence before authority, unchanged stopped recovery,
unchanged running behavior and failure stopping readiness. This proves ordering, not real ACLs.

## Failure model

- Actors and deployments: operators applying provider_authority_host on supported Linux hosts;
  CI executes the local harmless Ansible boundary proof without modifying installed services.
- Invariants and assets: socket input convergence precedes authority start; readiness follows
  service convergence; a failed libvirt restart prevents a successful readiness report.
- Accepted failure classes: actual systemd/libvirt/firewall failure remains a failed play;
  this change does not make unavailable infrastructure healthy.
- Covered elsewhere: broader clean-host matrix #2807; observer privilege reduction #3237.

## Validation and rollback

Extend existing check_authority_install.py; retain its real package-upgrade proof.
Align the existing deployment structural assertion to appliance verification before
handler convergence before explicit start; retain its appliance identity/access checks.
Run its ordering proof red before the move, then the entire existing command green;
run lint, whole-tree type and Ansible lint. Managed pre-push owns the final full gate.
Reverting the task move and its test restores the former ordering; no persisted migration.
