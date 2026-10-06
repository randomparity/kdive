# Native POWER spine kernel prerequisites

## Problem

The native POWER spine instructions accept a built ppc64le tree without stating the
network and debug information needed by its SSH and live-script proofs. A kernel with
`CONFIG_VIRTIO_NET=m` can boot without a loadable network driver, and the live-script
preflight requires `CONFIG_DEBUG_INFO_BTF=y`. The host role also omits `pahole`, which
the kernel build needs to produce BTF. BTF is not what drgn-live reads: drgn-live on an
uploaded kernel needs matching DWARF debug information readable inside the guest, and no
released drgn reads kernel BTF, so `CONFIG_DEBUG_INFO_BTF` alone does not make drgn-live
work. That capability premise and the live-script proof belong to #3121.

## Scope

The `live-testing.md` native spine section owns the operator-facing statement of the
kernel configuration. It names what each proof needs: `CONFIG_VIRTIO_PCI=y`,
`CONFIG_VIRTIO_BLK=y`, and `CONFIG_EXT4_FS=y` for the ext4 guest root;
`CONFIG_VIRTIO_NET` for the SSH proofs, built in or as a module the guest loads
before SSH; and `CONFIG_DEBUG_INFO_BTF=y` with a DWARF choice for the live-script
proof's preflight, stating that passing that proof also needs matching DWARF debug
information readable inside the guest (#3121). It agrees with the spine kernel preflight
(`2026-09-24-spine-kernel-preflight-design.md`), which already enforces these
symbols and accepts `VIRTIO_NET=y` or `=m`; this change does not alter the
preflight. It states that the pinned `fixtures/kernel/debug.config` fragment used
by `scripts/kernel_fixtures.py` already sets every symbol, with `VIRTIO_NET=y`. It
links to the external-build guide's debug-information section for the guest drgn
debug-information rules instead of restating them.

The external-build guide links operators to this native spine configuration beside
its general kernel-config advice, and its debug-information lines stop stating that
drgn reads BTF; both copies of the guide stay byte-identical. The
`local_worker_host` family package lists add the distribution package that
supplies `pahole`: `pahole` on Debian/Ubuntu and `dwarves` on Red Hat/openSUSE/SLES.
The shipped runner and local-libvirt plays also apply `libvirt_stack`, which already
declares `pahole`, but `local_worker_host` owns the live worker's kernel-debug toolchain
and must stand alone: a play that applies it, or `live_vm_host`, without `libvirt_stack`
otherwise leaves the host unable to build a BTF kernel. Existing OS package
tasks consume those lists. No new validator, guest image, build profile, or package
abstraction is introduced.

## Success

- A native POWER operator can configure the spine kernel for the SSH proofs and the
  live-script preflight from the runbook without inferring either requirement from a
  failed proof, and is not told that BTF alone makes drgn-live work.
- A host converged by `local_worker_host` has the package that supplies `pahole`
  on its supported Debian, Red Hat, and SUSE family package lists, subject to
  its configured OS repositories carrying that package.
- The external-build guide points to the native spine-specific requirements
  without presenting them as a general upload validation rule.

## Failure model

- Actors and deployments: local operator building a native ppc64le kernel; Ansible
  provisioning a live worker host on supported Debian, Red Hat, or SUSE families.
- Invariants and assets: SSH reachability of the proof guest; reproducible host tool
  provisioning.
- Accepted failure classes: a guest without matching DWARF debug information fails the
  live-script proof at `introspect-script` with `debuginfo_unloadable`; that proof and
  the drgn BTF premise belong to #3121. Kernels built outside the documented spine path
  retain their own config choices.
- Covered elsewhere: upload shape validation belongs to `runs.complete_build`;
  guest drgn distribution belongs to the guest image workflow; sibling spine
  failures belong to #2731–#2733.

## Validation

- Package contract: Mode: focused-test. `deploy/ansible/tests/run-local-worker-host.py`
  pins the Ubuntu list to its baseline plus `pahole`; expected red is a missing
  entry.
- Operator prose: Mode: task-test-not-applicable. No executable consumer parses
  the runbook or guide's instructional wording; manually inspect the rendered
  config list and link, then run `just docs-links`.
- Native proof: Run the SSH console-parts spine proof on a native POWER host with the
  pinned fixture kernel after the play installs `pahole`; required before merge. The
  live-script proof belongs to #3121.
