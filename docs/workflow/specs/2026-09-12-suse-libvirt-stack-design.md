# SUSE-family libvirt stack preparation

## Scope and authority

Issue #2392 supplies the role and harness criteria; the operator approved the sibling exclusions
and later offered an openSUSE Tumbleweed host for live validation. Frozen scope token:
`q2392-fe3256d4`. [ADR-0643](../../adr/0643-suse-libvirt-stack-modular-daemons.md) records the
daemon-model choice. Host-install playbook/docs and worker-host preparation belong to #2393/#2394
and #2391 respectively. The package list and daemon work remain in `libvirt_stack`.

## Problem

On a SUSE-family host, both package tasks and both daemon branches skip. The role then reaches
`virt-host-validate` without installing libvirt and reports a misleading KVM failure. An unknown
family follows the same path. The QEMU emulator map lacks `Suse` despite the repository's existing
openSUSE package guidance.

## Components and flow

At role entry, an assert accepts `Debian`, `RedHat`, or `Suse`; otherwise it names the received
family and asks for its virtualization package set. It runs before any package or service task.
The existing Debian and RedHat package tasks remain unchanged. A SUSE `community.general.zypper`
task installs `libvirt-daemon-qemu`, `libvirt-daemon-proxy`, `libvirt-client`, `qemu-tools`,
`guestfs-tools`, `e2fsprogs`, `virt-install`, `gnutls`, `libseccomp2`,
`python3-libvirt-python`, and `python3-lxml`, plus the mapped native emulator (`qemu-x86` or
`qemu-ppc`). The two Python names are zypper capabilities that select the distro's default
Python ABI; the Tumbleweed host resolves them to Python 3.13 bindings. `community.general` is
already pinned in `deploy/ansible/requirements.yml`.

RedHat and SUSE share the modular daemon block; Debian keeps the monolithic block. The modular
block masks monolithic units that systemd reports as loaded and enables the existing QEMU,
network, storage, node-device, secret, and proxy sockets. Tumbleweed's modular package set has no
monolithic units, so the role skips those masks on a clean host. The group task stays common but
selects the connection user, falling back to sudo's original user and then the gathered user when
no connection user is declared. Privileged fact gathering reports `root` on the offered host, so
the role must not choose root over its operator.
The KVM validation task stays common. The runner-task snapshot added
by #2391 must be updated for the new entry and SUSE package tasks.

## Success

- For `Debian`, `RedHat`, and `Suse` facts on `x86_64` and `ppc64le`, the harness observes the
  expected package list and only the selected daemon model; a privileged fact set still selects
  the named connection operator for group membership.
- For an unsupported family, the first role task fails with the family name and missing-set
  guidance, before a package, service, or KVM task executes.
- On the offered Tumbleweed host, the applied role resolves packages, enables modular
  sockets, leaves `libvirtd` inactive, adds the connection operator to `kvm` and `libvirt`, and
  reaches KVM validation.
- `just test-ansible`, `just lint-ansible`, and the role syntax check pass. Live SLES and Leap
  behavior is reported as untested.

## Failure model

- **Actors and deployments:** operator applying the role on Debian/Ubuntu, Fedora/RHEL,
  SLES/openSUSE, or an unsupported family; CI exercising fact-driven role resolution; the offered
  Tumbleweed KVM host for live validation.
- **Invariants and assets:** supported hosts receive their correct native emulator and daemon
  sockets; an unknown family fails before mutation; the live apply targets a host with no active
  libvirt guest or monolithic client; group changes affect the connection operator account.
- **Accepted failure classes:** unavailable distro repositories or KVM devices fail with Ansible's
  package/KVM diagnostics after family selection; the offered host can prove Tumbleweed only, not
  SLES or Leap. A unit-transition failure can leave a partial modular state. Repair the failed
  package/unit, then reapply the role; to restore a previously active monolithic model, unmask
  `libvirtd.service` and its sockets and enable/start `libvirtd.socket`. No host-independent
  harness claims live service success.
- **Covered elsewhere:** the canonical host-install entry point is #2393; operator docs are
  #2394; worker account and debug-tool provisioning are #2391.

## Threat model

- **Added boundary:** an operator-selected inventory host and Ansible-reported OS family/arch
  select privileged package and service changes; the top-of-role family assert and existing
  architecture-keyed emulator map govern those facts before mutation.
- **Actor model:** only the privileged operator can run this host role; tenant requests do not
  reach Ansible. Package repositories and inventory are trusted operator inputs.
- **Existing boundary widened:** SUSE joins the libvirt operator group and exposes the same
  local Unix modular sockets as RedHat. The role enables no TCP or TLS socket; the existing
  `libvirt_tls` role owns remote TLS exposure and still selects monolithic units for SUSE;
  `site.yml` is not a validated SUSE remote deployment path in this change.
- **Out of scope:** protection against a malicious local administrator or compromised distro
  repository is outside this role's trust boundary; remote access policy remains with
  `libvirt_tls` and the host-install owner.

## Validation

The new role harness reads the actual role YAML and evaluates its Jinja expressions and `when`
guards under injected facts without running package or service modules. Its unsupported-family
case checks the first task's refusal message and guard, and it checks operator selection with
`ansible_user_id=root` and `ansible_user` set to a non-root account. The existing runner harness
retains its exact task-order guard after
updating the expected task fixture. Ansible lint and syntax checks catch parse and style errors.
The live Tumbleweed apply, followed by package, socket, group, and `virsh` checks, is the one
end-to-end arm; its output must be reported separately from the host-free harness.
