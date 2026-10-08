# Ubuntu remote guest source routing

## Problem and scope

Implement #3091's image-owned networking under
[ADR-0763](../../adr/0763-ubuntu-remote-guest-network-policy.md).
Current Ubuntu boots QGA with no configured NIC. NetworkManager images already
have ADR-0721's source-route helper; Ubuntu has native event infrastructure but
no adapter. Bare has no sshd or network policy and is not a remote SSH image.

The explicitly approved scope amendment removes the DHCP default via the slirp gateway
on the identified slirp NIC. It preserves ordinary egress through the primary NIC.
The operator approved this precise exception after the full design review and scope audit.
All other main routes, domain XML, slirp topology/restrict mode, provider code,
kernel helpers #3081/#3082 and native POWER qualification remain excluded.

## Behavior and ownership

The existing image role selects Ubuntu by its catalog distro. Stage its owned
netplan DHCP policy, networkd adapter, shared unchanged source-route helper and
native-service startup replay drop-in through existing virt-customize operations.
Explicitly declare already-installed distro packages. Fail a missing native tool,
unrelated netplan file or failed install; never silently skip network policy.
No source on the host or active guest is patched during a build.

Use a virtio-driver match for DHCPv4; do not encode interface names, MACs or PCI
positions. Disable cloud-init network generation only; retain its other behavior.
Netplan syntax is validated during image preparation. Basic DHCP owns ordinary
connected/default routes; the source-route adapter owns table/priority2291 and the
approved narrowly identified restricted default suppression.

The adapter consumes native IFACE, STATE and IP_ADDRS. Configured/routable or startup
replay installs policy only for the one observed slirp IPv4 lease. Native address
replacement reaches configured again; the old rule is removed by the shared helper.
If no slirp address remains on that interface, invoke its guarded down path.
Off/no-carrier/failed/linger invokes the same down path. Irrelevant states do nothing.
Reject ambiguous multiple slirp addresses instead of guessing. Quote native values;
no eval, unbounded polling, externally configured script path or new secret surface.

For the matched slirp interface, inspect only main-table DHCP default routes through
the fixed slirp gateway. Absence is a no-op; remove the exact matching default only.
Static defaults or routes on another interface/gateway are untouched. Then call the
existing helper with the selected lease, preserving its failure status. A native
query failure is an error, not absence. The default suppression cannot run for a
non-slirp lease. The shared helper's existing table ownership/partial-update model
is retained, not strengthened into a new concurrency guarantee.

Hook files/directories satisfy native root:root0755 checks. One adapter is linked
into the named state directories. The native service drop-in calls the same daemon
with its existing optional arguments plus startup replay; no new daemon/loop.
Repeat image provisioning converges and retained staged images require explicit
force rebuild. Existing bare/Fedora/Rocky paths never receive Ubuntu policy.

Bare support is stated explicitly in its catalog entry and current operator docs:
no remote SSH parity; bootability remains unvalidated. No machine-readable key
that consumers ignore is introduced. No successful bare SSH claim or runtime
capability change follows from documentation.

## Failure model

Prevent policy on the wrong NIC, accidental static/default-route deletion, slot
coupling, hidden install failures and competing image network ownership. Detect
failed native commands and missing package/configuration with actionable build/hook
errors. Root-admin changes, multi-command atomicity and arbitrary custom images
remain outside the existing image contract. DHCP/dispatcher failures leave evidence
and do not become successful carrier results.

## Validation

Execute the adapter and existing helper with the ip boundary stubbed: slirp vs
primary, repeated events, changed lease, missing/multiple lease, teardown, absent
route, preserved static/other-default sentinels and injected native failures.
Run actual rendered role commands against isolated filesystem boundaries, proving
Ubuntu-only placement, root modes, startup replay, foreign-config refusal,
repeat preparation and failure propagation. Netplan's installed generator validates
the built image. Keep existing NetworkManager regressions unchanged.

Controlled omission of source-rule creation and wrong default selection must make
meaningful tests red. Focused deployment tests, role harness, lint/type/shell/Ansible,
records/docs and hooks precede commits; installed pre-push owns the sole full CI run.

Live proof uses a freshly built stock Ubuntu image at the exact candidate, with
matching server/worker/reconciler. Through real MCP: provision, authorize an agent
key, read ssh_info, authenticate over the remote forward, record OS/arch/boot ID,
check main/source routing and object-store egress, reboot, authenticate with the
same key and prove a different boot ID. Exercise native lease renewal, link
reactivation and dispatcher startup replay with the same assertions. Release and
verify domain/volume/capacity cleanup. Runtime-only task firewall and observer
credentials are explicit operator prerequisites, removed exactly after proof.
No unmerged #3081 helper is copied/cherry-picked into this candidate or image.

## Migration and checkpoint

Only rebuilt new images acquire this behavior. Preserve old image/evidence copies
before rebuilding the assigned task image. No broad guest or lab reset.
The complete design was reviewed and the scope audit identified one checkpoint. The
operator explicitly approved the native policy and exact restricted-default exception
before implementation; the resumed scope records that decision. Final review budget remains two passes.

## Observed SSH host-key prerequisite amendment

The first actual candidate provision reached ready with both DHCP leases and the
correct main/source routes, but forwarded SSH failed: vendor `sshd -t` reported
`no hostkeys available`. Cloud-init was disabled because this remote guest has no
cloud datasource. No image role or native SSH unit generates the missing keys.

For Ubuntu only, install an owned root:root 0644 `ssh.service` drop-in that resets
ExecStartPre, executes `/usr/bin/ssh-keygen -A`, then retains `/usr/sbin/sshd -t`.
The image build must require the vendor unit's single exact precheck, reject
additional/continued/unknown prechecks and foreign service drop-ins, and accept
only its byte-identical owned drop-in on repeat. Verify the native executable
exists. Do not generate keys while customizing the shared image, remove existing
keys, change authentication, or change general cloud-init policy.

Tests execute the actual rendered role command: wrong unit shape/foreign drop-in
fail before replacement; owned repeat succeeds; native generation precedes
validation; generation failure prevents validation. Verify key generation with
real OpenSSH in an isolated temporary root, existing key bytes unchanged across
repeat, and distinct keys for separate roots. Actual canonical rebuilt-image proof
must show no shared base-image keys, two guest clones with distinct fingerprints,
and persistent guest fingerprints across service restart and reboot. The required
SSH/routing/lease/reboot proof remains unchanged and must pass.

This bounded observed prerequisite is within the approved Ubuntu SSH-parity scope;
root accepted it for design amendment on 2026-10-08. Independent amendment review
precedes implementation; cumulative final-review budget remains 0/2.
