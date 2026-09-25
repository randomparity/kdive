# Native POWER spine kernel prerequisites

## Problem

The native POWER spine instructions accept a built ppc64le tree without stating the
network and debug information needed by its SSH and live-script proofs. A kernel with
`CONFIG_VIRTIO_NET=m` can boot without a loadable network driver, and a kernel without
BTF cannot supply the guest drgn script's debug information. The host role also omits
`pahole`, which the kernel build needs to produce BTF.

## Scope

The `live-testing.md` native spine paragraph owns the required kernel configuration.
State that this direct-boot tree needs `CONFIG_VIRTIO_PCI=y`, `CONFIG_VIRTIO_BLK=y`,
and `CONFIG_EXT4_FS=y` for its ext4 guest root. The existing spine preflight checks
fixtures, not these config symbols; the operator must check the built `.config`.
State that SSH proofs need `CONFIG_VIRTIO_NET=y`, or a matching module available
in the guest root filesystem or initramfs and loaded before SSH. The
live-script proof additionally needs
`CONFIG_DEBUG_INFO_BTF=y`, its DWARF prerequisite, a compatible guest drgn build,
and host `pahole` during the kernel build. Refer to the exact proof names.

The external-build guide links operators to this native spine configuration beside
its general kernel-config advice. The `local_worker_host` family package lists add
the distribution package that supplies `pahole`: `pahole` on Debian/Ubuntu and
`dwarves` on Red Hat/openSUSE/SLES. Existing OS package tasks consume those lists. No
new validator, guest image, build profile, or package abstraction is introduced.

## Success

- A native POWER operator can configure the spine kernel for SSH and live drgn
  from the runbook without inferring either requirement from a failed proof.
- A host converged by `local_worker_host` has the package that supplies `pahole`
  on its supported Debian, Red Hat, and SUSE family package lists, subject to
  its configured OS repositories carrying that package.
- The external-build guide points to the native spine-specific requirements
  without presenting them as a general upload validation rule.

## Failure model

- Actors and deployments: local operator building a native ppc64le kernel; Ansible
  provisioning a live worker host on supported Debian, Red Hat, or SUSE families.
- Invariants and assets: SSH reachability of the proof guest; BTF usable by guest
  drgn; reproducible host tool provisioning.
- Accepted failure classes: guest images whose drgn cannot read the produced BTF
  remain unsupported here because image compatibility belongs to that image.
  Kernels built outside the documented spine path retain their own config choices.
- Covered elsewhere: upload shape validation belongs to `runs.complete_build`;
  guest drgn distribution belongs to the guest image workflow; sibling spine
  failures belong to #2731–#2733.

## Validation

- Package contract: Mode: focused-test. Check the three family package lists
  name packages that supply `pahole`; expected red is a missing package entry;
  green command: `just lint-ansible`.
- Operator prose: Mode: task-test-not-applicable. No executable consumer parses
  the runbook or guide's instructional wording; manually inspect the rendered
  config list and link, then run `just docs-links`.
- Native proof: Run the SSH-dependent spine and live-script proof on a native
  POWER host with a rebuilt BTF kernel; this remains required before merge.
