# Live-stack kernel-tree preflight and provenance

## Problem

The live-stack spine stages and uploads any built kernel tree named by
`KDIVE_KERNEL_SRC`. A missing direct-boot driver or crash-capture symbol can first
appear as a failed boot, SSH timeout, or kdump job. The test report names the tree
but does not identify its built release, source revision, dirty state, or config.

## Scope

The shared upload entry point in `tests/integration/live_stack/spine.py` owns a
preflight before `combined_kernel_tar` and before `artifacts.create_run_upload`.
Read `.config` once per preflight and reuse `parse_kernel_config`. Require
`CONFIG_VIRTIO_PCI=y` and `CONFIG_VIRTIO_BLK=y`; require `CONFIG_EXT4_FS=y` for
ppc64le's documented ext4 guest and local x86-64 `build-fs` ext4 guests,
including the SUSE v7.0 proof. Remote x86-64 images with an unknown filesystem
must have at least one of `CONFIG_EXT4_FS=y` or `CONFIG_XFS_FS=y`.
SSH-dependent callers opt into the network check: reject `VIRTIO_NET=n` or absent
there, but accept `=m` because the guest may load the matching
module before SSH, as #2734 documents. The module's availability is an operator
image condition and cannot be established from the kernel `.config`.

Local callers pass their known `root_fs="ext4"`; remote callers retain the
filesystem alternative unless their fixture names a verified filesystem.
Only callers that request kdump set `require_kdump=True`. Reuse the existing
`crash_capture` feature clauses for that check, with the actual target arch:
`KEXEC` or `KEXEC_FILE`, `CRASH_DUMP`, `PROC_VMCORE`, `RELOCATABLE`, and
`FW_CFG_SYSFS` on x86-64. Do not require unconfigurable selectors. The SUSE
v7.0 proof reuses the shared boot check in place of its duplicated symbol list,
while keeping its version, ext4 rootfs, and built artifact checks.

Extend the existing live kernel-tree pytest header in `tests/conftest.py` with
`make -s kernelrelease`, a SHA-256 of the exact `.config` bytes, and, only when
that path is a Git root, `git describe --always --dirty` and a dirty state from
`git status --porcelain`. Header probes report unavailable values without
masking the upload preflight's named failure. The preflight refuses unreadable
config and missing symbols using `SpinePhaseError` with `phase_name` and symbol
names. The report publishes no local identity to GitHub.

No production upload policy, tree selection, kernel provisioning, dirty-tree
rejection, or generic bundle size gate changes.

### Failure model

- Actors and deployments: operator-supplied kernel trees for local and remote
  live-stack proofs on x86-64 and ppc64le; pytest on a host without a tree.
- Invariants and assets: upload begins only after readable config and required
  symbols; the header describes the same tree named by `KDIVE_KERNEL_SRC`.
- Accepted failures: a loadable virtio-net module may still be absent from the
  guest image; a filesystem choice different from the image can still fail
  boot; a dirty tree is reported but accepted; release and Git commands may be
  unavailable in the header and are reported as such.
- Covered elsewhere: the product's general upload and size rules, operator
  guest-image provisioning, and #2749's tree-selection output.

## Success

A live-stack upload with missing built-in root-device symbols stops in the
upload phase and names the missing `CONFIG_*` settings. Kdump proofs additionally
stop on the existing arch-aware crash-capture refusal set. A configured live
run's pytest output identifies release, source revision and dirty state when
available, and SHA-256 of the supplied `.config`.

## Validation

- Mode: focused-test. Config refusal and acceptance, including `=m`, local
  x86-64 XFS-only refusal, remote x86-64 XFS acceptance, ppc64le ext4,
  arch-aware kdump clauses, and no
  upload on refusal; expected red is acceptance of an invalid tree or refusal
  of a documented module route; green command:
  `uv run python -m pytest tests/integration/live_stack/test_spine.py -q`.
- Mode: focused-test. Header identity, absent Git, unavailable commands, and
  quiet-mode visibility; expected red is missing identity lines; green command:
  `uv run python -m pytest tests/integration/live_stack/test_skew.py -q`.
- Mode: focused-test. SUSE proof's retained version and boot checks; expected
  red is a wrong release or modular root-device driver accepted; green command:
  `uv run python -m pytest tests/integration/test_live_stack.py -k require_v7_0 -q`.
- Mode: task-test-not-applicable. No production upload contract changes; the
  source boundary is limited to the test harness and its pytest output.
