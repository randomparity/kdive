# SELinux label for the local image-build workspace — #2428

## Problem

`build-image.sh` sends its user-writable workspace to `build-fs`.  Under the local example's
session libvirt daemon, the customization domain opens its temporary disk and direct-kernel
`kernel`/`initrd` from that workspace.  On enforcing RedHat-family hosts, the workspace's normal
home-data label is not mappable by `svirt_t`, so the build fails even though the published rootfs
tree is already labeled.  ADR-0640 established `svirt_image_t` and the sourceable helper that
applies it; this issue makes the build path use that existing contract.

## Scope

The script will call `kdive_label_svirt_image "$workspace"` after creating the resolved workspace
and before its first `build-fs` invocation.  That function remains the only implementation of
SELinux detection, `semanage`, and `restorecon`; it is a no-op when SELinux is absent or not
enforcing.  Focused coverage will prove the build script has that ordered call and that the
existing helper still receives the resolved workspace.  The example README and provider guide will
replace their warning that host labeling excludes this build with accurate workspace guidance.

No ADR is added: ADR-0640 already governs the static label and its helper.  This change neither
labels provisioning or install trees (#2424), changes Ubuntu/AppArmor or remote-libvirt behavior,
nor adds a policy module.

## Failure model

### Actors and deployments

- A local operator runs the local-libvirt example on an enforcing Fedora or Enterprise Linux host.
- The operator-owned session daemon starts the customization domain; Debian/Ubuntu remains an
  existing no-op deployment.

### Invariants and assets at stake

- The selected workspace, including its temporary descendants, gets the existing
  `svirt_image_t` fcontext before the domain opens it.
- The script does not broaden labels beyond the operator-selected workspace or alter sVirt.

### Accepted failure classes

- A missing `semanage` reports the existing prerequisite and returns from the helper; host setup
  already installs that prerequisite on supported enforcing hosts.
- A failing `semanage` or `restorecon` stops the script before `build-fs`; this is safer than
  launching an unlabelled customization domain.

### Covered elsewhere

- Provisioning/install tree label ownership and its live proof: #2424 and ADR-0640.
- EL Python/libguestfs binding limits: provider guide's Enterprise Linux section.
- Ubuntu/AppArmor, remote-libvirt, and custom SELinux policy: frozen exclusions.

### Threat model

**Boundary inventory.** The existing operator-to-host-label boundary is extended to the one
operator-selected build workspace. No network, tenant, credential, or new process-input boundary
is added.

**Actor model.** The guest being customized is untrusted code running under QEMU; the local
operator and their selected workspace are trusted.

**Controls per boundary.** The helper takes one quoted directory argument, scopes the fcontext
pattern to that directory and descendants, requires enforcing SELinux before `sudo semanage`, and
propagates labeling failures. QEMU remains confined by the unchanged sVirt policy.

**Out of scope.** A malicious local operator can choose a sensitive workspace; that actor already
controls `KDIVE_BUILD_IMAGE_WORKSPACE` and can run `sudo`, so it is outside the named deployment.

## Success

1. For the resolved workspace used by `build-image.sh`, the helper runs after directory creation
   and before each possible customization boot through `build-fs`.
2. On the named enforcing deployments, the customization domain can map workspace-resident build
   inputs under the existing `svirt_image_t` policy; non-enforcing deployments run no label tools.
3. Operator documentation describes the workspace labeling and no longer calls this build path an
   unresolved SELinux defect.
4. The focused script tests, shell lint, documentation guards, and applicable repository checks
   pass.

## Validation

| Contract | Evidence |
|---|---|
| Workspace label occurs before `build-fs` | a static script-contract test that rejects removal or reordering |
| Existing helper labels a supplied directory and propagates failures | `tests/scripts/test_selinux_label.py` focused tests |
| Documentation links and paths remain valid | `just docs-links` and `just docs-paths` |
| Shell syntax and policy | `just lint-shell` |
