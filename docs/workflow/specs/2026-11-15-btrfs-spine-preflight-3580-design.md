# Name the btrfs driver in the spine kernel preflight (#3580)

## Problem

`check_spine_kernel_config` (`tests/integration/live_stack/spine.py`) rejects a kernel tree
that lacks the guest root's filesystem driver, but accepts only `root_fs` values `None`,
`ext4` and `xfs`. The Fedora cloud base used by the remote authority lane has a btrfs root.
A kernel without `CONFIG_BTRFS_FS=y` boots to `boot_timeout` with no named cause, and both
runbooks say so. #3519 added the driver to the pinned fragment (`fixtures/kernel/debug.config`)
but left the preflight unchanged until a live cell proves a fragment-built kernel boots that
base.

## Design

1. **Live cell first.** Build the `longterm` fixture with `scripts/kernel_fixtures.py build`
   into a fresh output directory on the control plane of the remote authority lane, and run
   `tests/integration/test_authority_vmlinux_live.py` with `KDIVE_KERNEL_SRC` set to that
   output. The authority's staged base is the Fedora 44 cloud image, btrfs root (checked with
   `virt-filesystems`). The cell passes when the activation boots the uploaded kernel to a
   responding guest agent and the test passes. Record candidate, fixture release, build ID and
   observations in the PR body and the issue.
2. **On pass only:** add `"btrfs"` to the accepted `root_fs` set and an `elif root_fs ==
   "btrfs"` branch that appends `CONFIG_BTRFS_FS=y` when `BTRFS_FS` is not built in, mirroring
   the `xfs` branch. The ppc64le/ext4 branch and the `None` default stay as they are. No caller
   changes: no existing caller passes `btrfs`.
3. **On pass only:** in `docs/operating/runbooks/live-testing.md` (fixture section) and
   `docs/operating/runbooks/remote-live-stack.md` (Prerequisites), replace the "no kernel
   preflight names the btrfs driver" sentences with the fact that the spine preflight names
   `CONFIG_BTRFS_FS=y` when called with `root_fs="btrfs"`. Keep the rebuild-into-a-fresh-
   directory guidance.
4. **On fail:** record the cause on the issue and in the PR; no `spine.py` or runbook edit.

## Failure model

- **Actors and deployments:** a lab operator or agent running live-stack tests from a
  control-plane checkout. CI never calls the preflight with a real tree.
- **Invariants:** the preflight's existing verdicts for `None`, `ext4`, `xfs` and ppc64le are
  unchanged; an unknown `root_fs` is still refused.
- **Accepted failure classes:** a kernel with btrfs only as a module (`=m`) is refused, the
  same rule as ext4/xfs, because the spine boots without a module-loading initrd guarantee.
  Callers that do not pass `root_fs="btrfs"` gain nothing; wiring callers is out of scope.
- **Covered elsewhere:** product-side kernel_config advisory for btrfs (#3582); nftables
  loopback proof (#3581).

## Validation

- `root_fs="btrfs"` accepted, missing driver named: `focused-test` —
  `tests/integration/live_stack/test_spine.py::test_spine_config_names_btrfs_for_btrfs_root`;
  red before the change with `unsupported guest root filesystem 'btrfs'`; green with
  `just test-verbose tests/integration/live_stack/test_spine.py`.
- Unknown `root_fs` still refused: `focused-test` — same test asserts `root_fs="zfs"` raises
  `unsupported guest root filesystem`.
- Live boot of a fragment-built kernel on the btrfs base: live cell in Design 1 (lab only).
- Runbook sentences: `task-test-not-applicable` — prose with no executable consumer;
  `just docs-links` and `just docs-paths` cover links and paths only.
