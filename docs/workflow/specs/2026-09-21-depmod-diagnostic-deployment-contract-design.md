# Depmod diagnostic deployment contract

## Scope

Register the local-libvirt diagnostic contribution only when local-libvirt is enabled.

## Decision

Use the existing `KDIVE_LOCAL_LIBVIRT_ENABLED` gate in provider diagnostics assembly. This preserves
ADR-0635's honest missing-depmod failure for enabled local-libvirt and ADR-0088's supported
container boundary. The four-directory resolution contract is unchanged.

## Verification

Test default registration and registration with local-libvirt disabled; run diagnostics tests and
the repository guardrails.
