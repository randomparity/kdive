# Remote external-boot initrd admission

Scope: issue #2795, `WORK:SCOPE` token `q2795-9f915ebc`; ADR-0682.

## Problem

Remote-libvirt has no provider-owned root device. Its plan retains the
filesystem `root=UUID=` token without an initrd, which the early kernel cannot
resolve. The failure currently arrives after activation.

## Scope

At `external_boot_root_arguments`, reject a missing or null initrd in build
evidence when the provider owns no root device. The MCP boot caller maps this rejection
to a configuration-error response before creating an activation. Tell the caller
to supply an initrd with the build. Preserve local whole-disk substitution and
the inspected token when an initrd exists. `RootSpecV1` and plan serialization
remain unchanged. Describe the refusal and recovery in the `runs.boot` wrapper
docstring, which is the agent-facing tool contract. PARTUUID provenance and
support belong to future remote work.

## Failure model

- Actors and deployments: authenticated `runs.boot` callers on a remote-libvirt
  stack; local-libvirt callers exercise the retained whole-disk path.
- Invariants and assets: refuse an unbootable remote root before activation;
  preserve the existing immutable plan shape and local root substitution.
- Accepted failure classes: remote direct boot without an initrd remains
  unavailable because no verified early-resolvable root token exists; malformed
  evidence outside the missing-or-null initrd case remains on existing validation
  paths because this change addresses only the supported no-initrd input.
- Covered elsewhere: post-admission activation and recovery failures remain
  with the external-boot job lifecycle (ADR-0583).

## Success

For the remote-libvirt no-initrd case, `runs.boot` returns a configuration error
with an initrd instruction and creates no activation. Remote builds with an
initrd and local builds with a provider device retain their root argument behavior.

## Validation

Focused service tests observe the remote rejection and both permitted root
argument cases. A focused MCP test observes a failure envelope and no activation
for the remote no-initrd case. Relevant lint, type, and focused tests pass.
