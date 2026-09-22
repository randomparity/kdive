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

| Criterion | Verification |
| --- | --- |
| Enabled local worker registers the diagnostic | `test_provider_diagnostics_registration_includes_local_and_remote_libvirt` |
| Disabled local provider omits it | `test_provider_diagnostics_omits_local_libvirt_when_disabled` |
| Every Compose app service renders the false flag | `test_app_services_disable_local_libvirt` |
| Helm keeps its false default | Existing Helm configuration guard |
| Debt 0014 has the exact resolution status | Debt records profile |
| The four-directory contract is unchanged | Existing depmod toolchain tests |

## Failure model

1. **Actors/deployment shapes:** reference Compose and Helm containers must not enable
   local-libvirt; enabled local worker hosts may.
2. **Protected invariants/assets:** provider-conditioned diagnostics, the Compose app-tier
   environment, Debt 0014's immutable body, and the four-directory depmod contract stay aligned.
3. **Accepted failures:** an enabled local worker without `depmod` fails honestly because module
   staging needs it; that is an actionable host-provisioning failure, not a container failure.
4. **Covered elsewhere/exclusions:** ADR-0088 owns kmod and container local-libvirt support;
   ADR-0631 and ADR-0635 own the depmod search directories. Neither is changed here.
