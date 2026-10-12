# 0759 — Refuse unsupported remote image publication at admission

## Status

Accepted (2026-10-12). Issue #3151; the operator approved this refusal on 2026-10-12.

## Context

`images.publish` authorizes an operator then unconditionally enqueues `IMAGE_BUILD`.
`image_build._resolve_catalog_build` rejects remote-libvirt before resolving its build
plane. An accepted request therefore creates a job that cannot succeed. The remote
plane uses local Fedora virt-builder and does not stage provider-pool volumes or install
the selected guest-family helpers. Its existence does not establish an operational
remote publishing pipeline. Operator image construction/staging remains the Ansible
image playbook described in the remote runbook.

Issue #3151 explicitly permits either implemented remote publication or an upfront
capability refusal. This decision narrows the managed publication promise of ADR-0092
and ADR-0461; it does not change operator image preparation or remote provisioning.

## Decision

After the existing platform_operator gate, `images.publish(provider="remote-libvirt")`
returns an error envelope with existing category `not_implemented`, retryable false,
object_id equal to the requested image name, and a constant explanation directing the
operator to the documented Ansible image build/staging procedure. No job reference or
jobs.wait suggestion is returned. Admission does not enqueue, write a catalog row or
object, or record a successful publication action. The current unauthorized-denial
response and audit behavior take precedence and remain unchanged.

Only remote-libvirt receives this new refusal. Local publication and deduplication,
unknown-provider behavior, and other providers remain unchanged. The worker's existing
rejection remains defense for old queued jobs; this change does not purge/rewrite jobs.
The remote build plane and production composition remain intact; removing them is not
necessary to correct admission and would change a separate port contract.

The MCP wrapper and provider Field description state the supported local provider and
remote refusal/recovery. Generated reference and CLI help are regenerated. The image
lifecycle and remote runbooks describe the same boundary.

The four x86_64 remote functional obligations retain their exact IDs, configurations,
exposures and carrier mappings. Their kind becomes `unsupported` with this ADR as the
reviewed rule; the carrier must actually call the tool as platform_operator through
both direct and gateway HTTP in default and recovery configurations, observe the
specific refusal and unchanged protected state, and record `unsupported` only after
those assertions pass. A static rule or skipped call is not proof. POWER obligations
remain present and unexecuted here; the same provider contract does not qualify POWER.
These named unsupported admission cells use server-only identity and actual server-host
OS/architecture, accelerator none, no guest identity or image/kernel inputs. Host architecture
continues to match each existing architecture-specific obligation, so x86 cannot qualify
POWER. No provider-host or guest observation is fabricated. Their binding path excludes
them from ordinary provider-lane image/KVM contexts. All existing authentication,
authorization and validation cells remain separate and unchanged.

## Alternatives

- **Judgment:** Implement remote build, guest-family helper installation, provider-pool staging and
  cleanup now. This creates new publication/installation ownership across hosts and
  requires a broader reviewed deployment contract. It is unnecessary for the explicit
  refusal option accepted by the issue.
- **Evidence:** Keep asynchronous failure. Rejected because it creates a guaranteed-failing job and
  misrepresents availability to callers. The actual admission and worker catalog-resolution
  functions reproduce this mismatch (build_publish.py::publish and
  image_build.py::_resolve_catalog_build).
- **Judgment:** Remove the remote plane or add a generic capability registry. Neither is needed for
  this single tool's concrete existing limitation.

## Consequences

Remote publication is explicitly unsupported through this tool, not silently omitted
from qualification. Remote operator image preparation/provisioning continues unchanged.
No authority deployment, new schema, dependency, configuration or migration is added.
Enabling remote publication later requires a decision and real build/staging/boot proof.
