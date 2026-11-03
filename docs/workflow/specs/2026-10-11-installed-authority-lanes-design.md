# Installed authority validation lanes

Scope: [#3183](https://github.com/randomparity/kdive/issues/3183), first child of #3152.
The frozen charter is `q3183-fc1232c3`; this design preserves its exclusions.

## Problem

The local authority host role is reachable through runner registration, while the ordinary
remote-provider lane must keep its plain-provider routing. Qualification needs separate installed
local and remote authority deployments whose revisions match the candidate.

## Design

Add `deploy/ansible/playbooks/installed-local-authority.yml`, targeting `live_vm_runners`
with an explicit inventory limit. Reuse `libvirt_stack`, `libvirt_pool_net`, and `live_vm_host`;
omit `github_runner`. Require an explicit operator input mapped to the existing host role,
full 40-character `live_vm_repo_version`, authority and worker-client enablement, mutation mode,
and a nonempty canonical System manifest before the roles mutate the host. Existing authority
preflight validates protected sources and base digests before the first role. Keep its existing
readiness, identity and worker/reconciler denial checks. Defaults and installers remain owned by
the existing roles. No responsibility moves and no compatibility path is removed.

Use existing `deploy/ansible/site.yml` and `provider_authority_host` for the separately assigned
remote target. Stage its clean exact-candidate checkout through the existing private infrastructure
workflow. There is no second remote installer and no registration of a GitHub runner.

The operator-approved root amendment (2026-10-12) places immutable remote module assets at
`/var/lib/kdive/remote-module-appliance/v1`, outside the restricted ordinary boot pool and the
mutable authority home. Change the fixed consumer default, installer default and both architecture
examples together. Authority-enabled remote-module preflight rejects a different destination.
Before starting the service or flushing its restart handlers, run the installed bounded loader
as the authority for each selected architecture. Keep the network-dependent one-shot CLI after
service start. No environment/API/schema, group membership, ordinary permissions or SELinux policy
changes are part of this correction. Preserve the old installed tree without a lookup fallback.
Separate exact-candidate deployment approval remains required; native POWER stays pending.

Extend `docs/operating/runbooks/live-testing.md` with the operator sequence: observe ownership,
snapshots, consumers, nested KVM, confinement and headroom; obtain exact restore/install approval;
reference protected DSNs/PKI/worker credentials, manifest/base digests and bounded capacity;
provision serially; start the existing host stack; record exact deployed identities; run bounded
readiness/denial proof and capture its terminal effects and exact-owned cleanup. Inventory and
raw evidence remain private. Snapshot names never establish deployed source identity. Add the
new playbook to the existing `just lint-ansible` syntax-check list.

## Success

- C1: the standalone local composition has authority preflight before host roles and no runner
  registration; existing remote composition remains the remote invocation.
- C2–C3: the runbook requires separate local, remote-authority and ordinary remote assignments,
  current resource observations, and concrete approval before restore or install.
- C4–C5: the local invocation rejects floating revisions and missing lane inputs; deployed server,
  each live worker, reconciler and selected authority identity must match the recorded candidate.
  Missing/skipped identity evidence cannot qualify a run. Protected inputs use existing contracts.
- C6: the installed readiness/denial result and any narrower blocker are recorded privately;
  proof-owned transient material and state have observed cleanup. Product tool qualification,
  System frames and fault construction remain #3184–#3186; native POWER remains #2818 pending.

## Failure model

- Actors and deployments: an attended trusted operator on exclusive disposable Ubuntu x86_64
  local-authority and Fedora x86_64 remote-authority hosts; one live scenario at a time.
- Invariants and assets: private credentials and infrastructure identity; ordinary remote routing;
  exclusive host state; exact candidate evidence; terminal proof effects and owned cleanup.
- Accepted failure classes: unavailable protected inputs or upstream product defects produce a
  named blocker, never passing qualification. Existing role preflight/installed gates own input
  validation and host isolation. Ubuntu session guests lack per-domain AppArmor confinement
  (ADR-0741); authority file/socket isolation is not guest sandboxing.
- Covered elsewhere: admission #3138; exhausted recovery #3143; provision/machine identity
  #3144/#3148; vmlinux #3130/#3131 and proof #3135; System frame #3184; faults #3185;
  HTTP cells #3186; native POWER #2818. Coverage #2803 remains unchanged; no release publication.

## Validation

Test fixed-root parity across the consumer, installer and both architecture examples. Exercise
destination mismatch rejection and disabled-mode behavior. The existing deployment harness runs
the real loader command over canonical fixture assets: valid bytes proceed, corruption or denied
ancestor traversal prevents the following start marker. Validate as an unprivileged identity,
including privilege drop when the harness runs as root. Actual QEMU access and guest confinement
at the new root remain System qualification obligations under #3184.

Extend provisioning structural tests for runner-free composition, preflight ordering, required
lane inputs and the syntax-check gate. Observe the new test fail before adding the playbook;
exercise a controlled removal of the preflight to prove the boundary test detects it. Run focused
provisioning tests, lint, whole-tree types, Ansible lint/syntax and doc gates. The mandatory push
hook owns full local parity. After separate installation approval, reuse installed role readiness
and denial checks, exact deployed revision probes and bounded cleanup evidence on assigned hosts.
