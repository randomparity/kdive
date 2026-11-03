# Skip authority-less remote module-volume hosts

## Problem and scope

Issue #3087 reports repeated failed maintenance jobs for valid inventory entries
without optional provider authority. The current callback checks authority after
opening libvirt, and raises conflict for this supported configuration.
The authorized scope is fleet selection and tests; deletion identity/retention stays
unchanged, authority deployment remains operator-owned, and DWARF belongs to #3131.

## Design and success

Filter the one `connections.configs()` snapshot to entries whose authority is not
None before passing it to `map_over_fleet`. Use that same eligible snapshot for the
existing no-reachable-host check. Empty/authority-less fleets return zero without
opening connections, resolving credentials, reading retention or deleting volumes.
Mixed fleets preserve eligible declaration order and count their actual results.
The existing callback guard for absent sender/identity remains unchanged.
Configured but unreachable eligible fleets still raise infrastructure failure;
reachable configured hosts without a sender still raise conflict.
No shared fleet helper, inventory, protocol, database or deletion-policy change.
Existing ADR-0588/0603 ownership and optional-authority configuration suffice;
reserved ADR0749 is unused because this restores the existing contract.

## Validation

Owning fleet tests cover empty, authority-less and mixed snapshots, both orderings,
no I/O for skipped hosts, missing sender, unreachable eligible hosts and one read.
Existing attachment/retention/deletion tests remain selected. A production-assembled
registered worker job uses disposable Postgres and the real queue/retention reader;
only external libvirt/credential boundaries are instrumented. It proves successful
skip and mixed-fleet dispatch, not a live remote TLS handshake or remote deletion.
Focused tests, lint/type/hooks and the managed full pre-push gate precede delivery.

## Failure model

- Actors/deployments: production reconciler and fixed worker processing maintenance
  jobs for validated remote-libvirt inventories on supported Linux architectures.
- Invariants/assets: no deletion or credential/network effects for ineligible hosts;
  configured-host failures and existing attachment/retention safeguards preserved.
- Accepted classes: inventory changes after the single snapshot take effect next
  sweep; inherited per-host connection failure isolation remains unchanged.
- Covered elsewhere: authority deployment/operator, identity/retention ADR-0588/0603,
  DWARF #3131, native POWER qualification #2818. No new failure acceptance.

## Threat model

- Boundary: validated fleet configuration selects existing privileged reaping and
  credential/network paths. This change narrows that selection, adding no input.
- Actors: operator controls inventory; workers use existing authority credentials;
  untrusted guest storage/volume metadata remains governed by existing reaper checks.
- Controls: skip before connection creation; retain configured-host sender guard,
  deletion identity/retention implementation and existing fleet error semantics.
- Out of scope: compromised privileged operator/authority and policy changes to
  deletion or retention; this filtering correction introduces no such authority.
