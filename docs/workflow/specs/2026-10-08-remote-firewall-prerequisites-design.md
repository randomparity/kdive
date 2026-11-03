# Remote-provider firewall prerequisites

## Scope and authority

Issue #3083 requires the existing ACL role to converge a clean RedHat provider in one
site.yml run, check Debian's equivalent prerequisite and report firewall failures accurately.
Campaign scope6051266264 preserves network-topology redesign and remote debug #3113 exclusions.
The operator already approved these boundaries. This is implementation of the existing ACL
contract, not a new port/source/authority policy. Optional ADR0745 is consumed unused.

## Cause and ownership

The current gdbstub_acl imports authority.yml before either baseline backend block. That file
validates ownership and then calls firewall modules without installing their backend. A read-only
Ansible check-mode firewalld invocation on the clean Rocky10.2 x86 provider failed with the actual
missing Python firewall import, changed=false. No firewall package or service was changed.
The outer provider_authority_host rescue currently mislabels this as authority readiness failure.

Keep backend prerequisites in gdbstub_acl, after authority ownership validation and before the
first authority rule operation. Add an imported prerequisites.yml there: RedHat installs
firewalld and python3-firewall with state=present, then reads service facts. Before activating an
inactive daemon, preserve the existing inventory-selected management TCP port (ansible_port,
default22) in the same default zone using permanent=true, offline=true, immediate=false.
Reject invalid or TLS/gdb-overlapping management ports before granting access; authority ownership
validation already rejects authority/management collisions. A failed offline grant prevents startup.
Then enable/start firewalld. Already-running backends retain their existing management policy;
no new management grant or reload is applied to them. Protected-port source restrictions stay intact.
The installed module writes disk configuration when offline, which daemon startup loads. Debian
installs ufw with state=present; existing management allow/rule ordering and activation stay in
main.yml. The inherited authority_firewall tag includes these prerequisites, so tagged authority
reconciliation cannot evade them. Unsupported-family behavior is unchanged.

Do not move provider_authority_host preflight or prepare.yml: input validation remains before
quiescence; an already-running authority is stopped before its changed ACL is reconciled.
Standalone gdbstub_acl still validates retained ownership before installing packages or starting
a service. Backend failures stop before rule mutation and retain the existing outer stop-on-error.

Within the provider convergence block, record one bounded phase string before the firewall role
and update it before authority installation/disabled cleanup. Rescue uses that string and says
any installed authority service remains stopped; it must not echo failed task arguments, secrets
or host-specific module output. Existing unit inspection/conditional stop remains unchanged.

## Success and validation

A fresh supported RedHat provider lacking both packages completes one canonical site.yml run;
firewalld is enabled/running, requested rich rules are persistent and active, selected-client
traffic is admitted and off-CIDR traffic rejected for TLS and a controlled gdb-range listener.
Use the assigned Ubuntu client as allowed /32 and the controller as off-CIDR probe; no guest debug
session or network-policy widening is required. Record exact candidate, inventory/input identities,
package/service observations and actual run status. A repeat run confirms convergence separately.
Use a nondefault direct management SSH port for the clean invocation and verify a fresh authenticated
connection afterward with connection multiplexing disabled. Preserve the prior SSH listener for
operator recovery; listener/SELinux preparation is fixture setup, not product behavior. A surviving
Ansible control socket alone is not evidence of management reachability.
A missing or failed later site step remains an actual failure, never a full-site pass.

Extend the existing real-role Ansible harness by replacing only package/service/firewall external
effects. Missing backend state must reject the first authority firewall call. Cover both families,
first install, nondefault/default management grants before RedHat startup, unchanged running-backend
management policy, protected-port collision rejection, idempotence, failed offline preparation,
failed package/service operations and no
backend mutation on invalid ownership. Exercise the real rescue block with controlled external
failure and assert the bounded firewall phase even when authority is disabled. Existing drift,
retraction, protected-rule and prune cases remain unchanged. Add structural owning tests only for
backend task placement/activation ownership and phase ordering not already observed by the harness.

## Failure model

- Actors: trusted operator inventory and distro package repositories; privileged Ansible modules
  configure the task provider under the existing authority-validation and ACL boundaries.
- Assets/invariants: source-restricted protected ports, management reachability, retained authority
  ownership, failure propagation and stopped authority on failed convergence.
- Required handling: package/service failure prevents rule use; malformed retained ownership has
  no backend effect; rescue reports the bounded failed phase; prior authority stop behavior remains.
- Accepted: unavailable repositories or unsupported platform/package layouts fail provisioning.
  This change does not promise transactional rollback of installed packages or daemon startup.
  It does not redesign existing rules, migration of stale RedHat source grants, or guest topology.

The fixture's separately operator-staged campaign-guests network avoids nested NAT overlap through
existing libvirt_network input; its preparation is not product behavior or firewall proof.
No native POWER result follows from x86 testing. No unrelated authority-lane scope is included.
