# Remote Docker/libvirt egress preflight

## Problem and scope

Issue #3093 permits scoped repair or actionable preflight. Choose a bounded
read-only check for the reproduced conflict, preserving administrator firewall
ownership. The native x86_64 reproduction used Docker with iptables-nft and
libvirt's separate native nftables rules: a routed guest request timed out while
the host request succeeded, with matching libvirt accept and Docker drop counts.
A local fixture booted directly supplies this diagnosis only; it is not a remote
helper or remote-install qualification.

The frozen campaign scope excludes broad forwarding relaxation and guest-family
work owned by #3081/#3082. Native POWER obligations remain. ADR-0751 records the
choice. No runtime tool schema, inventory field, network topology, shared-host
policy, or authority ownership changes are proposed.

## Behavior and ownership

Keep `libvirt_pool_net` as the owner of guest-network checks. Add a separately
invoked task file, imported by remote `site.yml` after protected-port/authority
firewall convergence and before `remote_libvirt_facts`. The shared role's normal
entrypoint and local-host play remain unchanged.

Use read-only commands to discover `iptables`, then read `iptables -S FORWARD`.
If no executable exists, report that this iptables-specific check does not apply;
no package is installed. Command failures when it exists fail with inspection
context, never a fabricated empty policy. Only when the ordered forwarding rules
are exactly the observed policy and two Docker jumps, read `DOCKER-USER`.
Recognize an empty chain declaration, optionally followed by unconditional
`RETURN`, as the conflict signature. Fail with operation, selected network,
Docker `FORWARD DROP` interaction, and the runbook remedy. A changed chain/read
failure fails inspection rather than becoming success.

The check does not claim that every matching signature proves a blocked flow in
an arbitrary customized downstream Docker chain. It refuses this known-risk
configuration until the administrator configures and verifies scoped egress.
Additional forwarding rules, custom user rules, non-Docker layouts, and IPv6
are not interpreted or certified. Success text must say the known signature was
not detected, not that guest egress works. No generated inventory changes.

The runbook requires an administrator-owned exception scoped to the selected
guest bridge/subnet, intended object-store destination and port, and established
return traffic. Preserve unrelated forwarding, protected-port source ACLs and
management access. Do not suggest `FORWARD ACCEPT` or disabling Docker rules.
The operator retains persistence/reload responsibility and must rerun setup and
actual guest transfer/install checks after firewall changes.

## Success

- The observed native conflict makes the canonical site run fail actionably
  before inventory emission, without changing firewall rules.
- Nonmatching policy layouts are reported as unverified by this bounded check;
  existing custom policies are not rewritten.
- Missing tooling is distinguished from failed inspection. The local-host play
  remains unaffected; the network name is data, never shell source.
- Focused regression and role tests prove ordering, exact signature boundaries,
  read failures, no mutation, and actionable failure text.
- Actual guest-to-store and remote-install results are retained separately.
  If external prerequisites prevent them, park their obligation truthfully;
  neither this preflight nor the outbound reproduction substitutes for a pass.

## Failure model

Detected conflict: fail and direct the operator to scoped configuration and live
verification. Missing iptables: signature not applicable, no claim about nft-only
policies. Inspection failure or concurrent chain removal: fail visibly. Custom
policies, late Docker/firewall changes, DNS and store availability: accepted
limits of this static check; actual guest proof remains required by #3093/#2803.
No policy rollback is needed because this change writes no host firewall state.

## Validation

Record the native red reproduction, then exercise candidate `site.yml` on that
same provider and verify the expected preflight rejection and unchanged rules.
Use boundary-stub role tests for absent tooling, inspection failures, exact
signature, extra custom rules and non-Docker hosts; deliberately disable the
assertion to show the regression fails. Run lint, whole-tree type, Ansible and
doc/record guards. The installed pre-push hook owns the full local CI suite.
Final independent and security review use the frozen candidate; final budget two.
