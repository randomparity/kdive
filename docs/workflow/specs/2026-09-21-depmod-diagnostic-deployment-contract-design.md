# Depmod diagnostic deployment contract

## Scope

Register the local-libvirt diagnostic contribution only when local-libvirt is enabled.

## Actors and invariants

Reference Compose and Helm/container deployments are remote-libvirt/fault-inject shapes and must
disable local-libvirt. An enabled local worker host retains the diagnostic and its honest missing
`depmod` failure. ADR-0088 owns container local-libvirt support; ADR-0631 and ADR-0635 own the
four-directory search, which this change does not alter.

## Decision

Use the existing `KDIVE_LOCAL_LIBVIRT_ENABLED` gate in provider diagnostics assembly. This preserves
ADR-0635's honest missing-depmod failure for enabled local-libvirt and ADR-0088's supported
container boundary. The four-directory resolution contract is unchanged.

## Verification

Test default registration and registration with local-libvirt disabled; run diagnostics tests and
Compose configuration tests, records, and the repository guardrails.

## Failure model

An omitted Compose flag re-enables the default-local diagnostic in supported containers. A
structural assertion over every app service prevents that deployment-contract regression.
