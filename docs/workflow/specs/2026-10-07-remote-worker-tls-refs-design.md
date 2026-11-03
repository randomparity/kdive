# Remote worker TLS ref provisioning

## Problem and scope

Issue #3088 completes the TLS file delivery deliberately deferred by #3086.
ADR-0574 already fixes the secrets root, provider group and remote TLS modes.
The runbook describes manual installation; neither host role accepts these sources.
The approved campaign scope covers provisioning and proof, excluding certificate
issuance/rotation, new secret backends and native POWER qualification (#2818).

## Design

Extend the existing `local_worker_host` owner, reused by `live_vm_host` through
`worker_install_dirs.yml`. Add three optional controller-local source paths, each
an empty string by default:

- `local_worker_host_remote_libvirt_client_certificate_source`
- `local_worker_host_remote_libvirt_client_key_source`
- `local_worker_host_remote_libvirt_ca_certificate_source`

Supplying at least one selects TLS installation and requires all three nonempty.
An operator configuring a remote instance supplies these inputs in the same host
provisioning invocation. No role reads or rewrites the runtime inventory. Empty
inputs preserve local-only provisioning and leave previously installed TLS files
alone; this is selection, not automatic credential deletion.

A reusable `remote_libvirt_tls.yml` import runs before the existing directory
creation loop. It checks the three sources on the controller with no link following:
regular, nonempty, and private (0400 or 0600), matching the existing authority TLS
source pattern. Source checks and copies use `no_log: true` and copy uses `diff: false`.
It inspects the fixed secrets parent and remote child without following links and
rejects an existing non-directory or symlink before creating either path.
It then creates `/var/lib/kdive/secrets` root:root 0711 and its `remote-libvirt`
child root:provider-group 0750, and copies certificate/key/CA as `clientcert.pem`,
`clientkey.pem`, `cacert.pem`, root:provider-group 0440 with destination `follow: false`.
Existing correct inputs yield no TLS changes on repeat application.

The normal runner and standalone role already prepare provider groups before this
entrypoint. For installer-only hosts, the documented one-shot Ansible entrypoint
loads `local_worker_host` with `tasks_from: remote_libvirt_tls.yml` after the existing
lifecycle installer has created its provider group. It creates the missing parent
without applying packages or changing the lifecycle shell installer. All three
sources supplied directly to this entrypoint remain mandatory.

See [ADR-0762](../../adr/0762-remote-worker-tls-provisioning.md).

## Success and validation

- Both host role paths contain the same TLS import; the runner ordered-task fixture
  records it. Empty source inputs skip the TLS tasks without installing files.
- Incomplete, missing, linked, empty or unprotected sources fail before destination
  creation; a substituted parent or child fails without touching its target.
- Installed bytes and modes match the three sources and ADR-0574. Group members can
  read but not modify; an outsider cannot read the key. Repeat installation is stable.
- Real x86_64 fixed slot identities read the installed refs and establish a TLS
  libvirt connection to the assigned provider, using existing private campaign PKI.
  Run the affected required remote carrier once host ownership is handed over; a
  separate prerequisite failure remains a failed or blocked carrier, never green.
- Focused actual Ansible fixtures, runner fixture regeneration, lint/type/role/hooks,
  then managed pre-push and remote CI cover the resulting branch.

## Failure model

- Actors and deployments: trusted root-capable Ansible operator/controller; fixed
  workers and the existing provider group on supported native Linux worker hosts.
- Assets/invariants: client key confidentiality, existing fixed secret refs and
  permission layout, local-only behavior, source bytes, unrelated services/files.
- Accepted classes: a privileged administrator racing metadata checks can substitute
  paths; root/controller compromise is outside this provisioning boundary. Three
  file copies are not transactional; an I/O failure may leave a partial update, so
  rerun with the same validated inputs. Certificate rotation coordination is excluded.
- Covered elsewhere: certificate issuance/expiry/chain by the operator PKI; provider
  reachability/firewall #3083; runtime ref confinement and worker inventory ADR-0574;
  native POWER proof #2818. No change promises hostile provider-group isolation.

## Threat model

- Boundaries: controller source files cross privileged Ansible copy into fixed host
  credential paths; the existing provider group receives read-only access. No new
  runtime secret backend or worker privilege is introduced.
- Actors: local outsiders and compromised worker processes are untrusted; the
  controller and root administrator are trusted. Provider-group peers already share
  the remote client identity under ADR-0574.
- Controls: no-follow source/directory inspection, source completeness/private modes,
  fixed destination names, root ownership, 0750/0440 and non-following copies;
  no_log and disabled diffs suppress paths/content in sensitive task output.
- Out of scope: root/controller races, PKI issuance/rotation and TLS validation policy,
  hostile peers holding the same client identity, automatic inventory discovery.
