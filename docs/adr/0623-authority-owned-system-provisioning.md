# 0623 Authority-owned System provisioning

## Status

Accepted (2026-09-06)

## Context

Authority-enabled external boot makes a provider authority the sole mutator of a
private libvirt daemon and storage root. Ordinary local provisioning opens the
worker's configured libvirt URI, and ordinary remote provisioning opens the
Resource's worker TLS endpoint. A System created there is invisible to the
authority's private session daemon. An operator-provisioned fixture proves live
acceptance but is not a production `systems.provision` path.

The existing external-boot authority ledger requires an activation, Run, and
plan. Initial System provision has none of those identities.

## Decision

An authority-enabled local-libvirt or remote-libvirt Resource routes initial
System provision through a distinct activation-free authority lane. The lane has
exactly two operations: `provision` and `preactivation-teardown`. It uses one
System ownership row, one generation-attempt table, one per-System authority
journal chain, and immutable canonical terminal receipts. It does not widen the
activation-bearing external-boot ledger.

Provision must commit an authenticated `provision-ready` receipt before the
first external-boot activation. First activation and preactivation teardown are
serialized under the System lock. Once activation wins, existing external-boot
authority and migration 0147 own teardown. Reprovision is unsupported for an
authority-owned System.

The authority resolves the canonical stored profile, immutable root provenance,
and durable System bootstrap public key from least-privilege database functions.
It maps their digests and the Resource binding to operator-owned private host
configuration. Requests never carry paths, libvirt URIs, XML, credentials, or
provider ports. Local and remote providers mutate only the authority's fixed
private connection and persist exact private intent before mutation.

Ordinary Resource behavior remains unchanged when the authority binding is
absent.

## Consequences

Migration 0149 adds two protected tables and closed role-specific functions.
Generic worker completion, failure, and abandoned-job repair cannot finalize a
marked operation. A completion-owned authority task may outlive a caller or job
lease long enough to persist its terminal receipt; a worker or reconciler can
later consume only those exact authenticated bytes.

An operational provision exception remains unresolved at the durable mutation
anchor. Recovery observation either proves readiness or retains quarantine;
neither path fabricates a failure category or completion timestamp. Capacity
and ownership remain until explicit preactivation teardown proves absence.
Retained quarantine never performs core cleanup or releases capacity. Provider
deployment must stage the same digest-pinned base directly into the authority
store.

### Amendment (2026-10-21): libvirt must not relabel the staged base (#3315)

This adds a deployment contract to "Provider deployment must stage the same digest-pinned base".
It does not change the provisioning lane. The authority hashes each staged base at startup and
then compares its device, inode, size, mtime and ctime on every readiness check and before
every provision. Any difference exits the authority. ctime stays in that comparison because an
actor with the authority's uid can rewrite a base and restore its mtime, but cannot restore its
ctime. Under the session daemon, qemu runs with that uid.

libvirt's SELinux driver labels every non-top backing-chain image `virt_content_t` each time a
domain starts. It calls `setfilecon` without comparing the current label and never restores a
read-only image at stop. setxattr changes ctime even when the value is the same. The session
daemon runs as the authority account, which owns the base, so the relabel lands, and the
authority exited once after every System start. The live run that found this was a remote
authority on Fedora with SELinux enforcing.

The contract is now:

- The base carries `virt_content_t` before any domain uses it. A confined `svirt_t` domain may
  read and map that type but not write it. The `provider_authority_host` role writes an
  exact-path fcontext rule for each staged base and runs `restorecon` on it wherever `getenforce`
  reports SELinux enabled. A relabel changes ctime, so the role restarts the authority.
- libvirt must not relabel the base. Local and remote authority domains render the base as an
  explicit `<backingStore>` whose source carries `<seclabel model='selinux' relabel='no'/>`. The
  overlay keeps libvirt's dynamic label. The authority's retained-domain identities include that
  seclabel, so a definition without it, or with `relabel='yes'`, reads as drift.
- The seclabel is inert on AppArmor and unconfined hosts. libvirt ignores a `relabel='no'`
  seclabel for a security model it has not loaded, and its AppArmor driver changes no file
  metadata.

Ordinary local and remote Systems are unchanged. Their daemons relabel the backing chain
themselves, and ADR-0640 rejects a `relabel='no'` seclabel for them. An authority domain is
always on its private session daemon, where that rejection's reason, a privileged daemon's
relabel, does not apply. A base that is relabelled out of band, such as by a `restorecon -F`
under a different rule, fails the next domain start with a qemu permission error. The authority
also exits until it is restarted, which re-hashes the base. Considered: dropping ctime from the
comparison. Rejected, because ctime is the field that catches the same-uid rewrite described
above.

## Considered & rejected

- **Provision through the worker and hand the domain to the authority.**
  **verified:** `LocalLibvirtProvisioning.from_env()` opens
  `KDIVE_LIBVIRT_URI`, remote provisioning opens the Resource TLS endpoint, and
  authority host composition opens its fixed private session socket. Libvirt
  domain ownership does not transfer across those daemons.
- **Put provision into the external-boot authority ledger.** **verified:**
  migrations 0122 and 0123 require activation, Run, and plan identities in both
  authority bindings and journal records; provision has none.
- **Add separate counter, acknowledgement, head, receipt, and audit tables.**
  **judgment:** one ownership row and one attempt-history row preserve the same
  state and replay evidence with fewer transactional joins and grants.
- **Let the request carry a profile or provider topology.** **verified:** the
  canonical profile and root provenance are already immutable database facts,
  while topology is operator configuration. Request copies would introduce a
  second, attacker-selectable authority.
- **Support authority-owned reprovision in the same lane.** **judgment:** it is
  a destructive replacement contract with snapshot and active-Run consequences,
  not necessary for initial production provisioning.
