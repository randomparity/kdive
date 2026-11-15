# Plan: name the btrfs driver in the spine kernel preflight (#3580)

Goal: once a live cell boots a fragment-built kernel on the btrfs-root Fedora base, make the spine
preflight accept `root_fs="btrfs"` and name `CONFIG_BTRFS_FS=y`, and correct two runbooks.
Architecture: one branch added to `check_spine_kernel_config`; no caller changes.
Tech stack: Python 3 test harness (`tests/integration/live_stack`), pytest, Markdown runbooks.
Spec: `docs/workflow/specs/2026-11-15-btrfs-spine-preflight-3580-design.md`.

Expected implementation size: 25–40 changed lines (S) — one branch plus one set member, one test, four runbook lines.

## Global Constraints

- Tasks 1 and 2 run only after Task 0 passes. If Task 0 fails, stop after recording its cause.
- Do not change the ppc64le/ext4 branch, the `None` default, or any caller of the preflight.
- Guardrails: `just test-verbose <paths>`, `just lint`, `just type`, `just docs-links` and `just docs-paths`.
- Public text names lab hosts only by redaction tokens (`sys-C`, `sys-A`).

## Task 0: live cell (lab, no repository change)

Run `tests/integration/test_authority_vmlinux_live.py` on the remote authority lane with
`KDIVE_KERNEL_SRC` set to the output of `python3 scripts/kernel_fixtures.py build --baseline
longterm --arch x86_64 --source <new dir> --output <new dir> --jobs <n>`, against the staged
Fedora 44 cloud base; confirm its btrfs root first with `virt-filesystems -a <base> --filesystems`. Expected: `1 passed`, and a
`vmlinux-staging-observed` line whose `kernel_release` is the fixture release. Also run the
branch's preflight against a copy of that tree's `.config` with `root_fs="btrfs"` (expect bytes
returned) and with `BTRFS_FS` unset (expect `SpinePhaseError` naming `CONFIG_BTRFS_FS=y`).
Cleanup: release everything, roll both lab guests back to their pre-run snapshot outside the
nightly backup window. On failure, record the cause in an issue comment and the PR.

## Task 1: preflight accepts btrfs

Files: modify `tests/integration/live_stack/spine.py` (`check_spine_kernel_config`); test
`tests/integration/live_stack/test_spine.py`.

Interfaces: consumes `spine.check_spine_kernel_config(kernel_src: Path, arch: str, phase_name:
str, *, ..., root_fs: str | None = None) -> bytes` and `SpinePhaseError` (both exist on main).

Verification:
- Contract "btrfs accepted, driver named; unknown still refused" — `Mode: focused-test`;
  test `test_spine_config_names_btrfs_for_btrfs_root`; red: the first `pytest.raises` fails with
  an `AssertionError` that its pattern did not match "unsupported guest root filesystem 'btrfs'"; green:
  `just test-verbose tests/integration/live_stack/test_spine.py` all pass.

Steps:
1. Add after `test_spine_config_scopes_filesystem_to_guest`:

```python
def test_spine_config_names_btrfs_for_btrfs_root(tmp_path: Path) -> None:
    config = tmp_path / ".config"
    config.write_bytes(b"CONFIG_VIRTIO_PCI=y\nCONFIG_VIRTIO_BLK=y\nCONFIG_EXT4_FS=y\n")
    with pytest.raises(SpinePhaseError, match="CONFIG_BTRFS_FS=y"):
        spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build", root_fs="btrfs")
    config.write_bytes(b"CONFIG_VIRTIO_PCI=y\nCONFIG_VIRTIO_BLK=y\nCONFIG_BTRFS_FS=y\n")
    assert spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build", root_fs="btrfs")
    with pytest.raises(SpinePhaseError, match="unsupported guest root filesystem"):
        spine.check_spine_kernel_config(tmp_path, "x86_64", "upload-build", root_fs="zfs")
```

2. Run the focused command; expect 1 failure in the new test.
3. In `spine.py` change `if root_fs not in {None, "ext4", "xfs"}:` to
   `if root_fs not in {None, "ext4", "xfs", "btrfs"}:` and insert after the `xfs` branch:

```python
    elif root_fs == "btrfs":
        if not config.is_builtin("BTRFS_FS"):
            missing.append("CONFIG_BTRFS_FS=y")
```

4. Run the focused command; expect all pass. Run `just lint` and `just type`; expect exit 0.
5. Commit `test(live): name CONFIG_BTRFS_FS in the spine preflight (#3580)`.

## Task 2: runbooks state the preflight

Files: `docs/operating/runbooks/live-testing.md` (paragraph starting "The default fragment also
builds in `CONFIG_BTRFS_FS=y`"); `docs/operating/runbooks/remote-live-stack.md` (Prerequisites
bullet ending "the boot job ends `boot_timeout`").

Verification:
- Contract "runbook wording" — `Mode: task-test-not-applicable`; prose with no executable consumer.

Steps:
1. live-testing.md: replace "the boot job ends `boot_timeout` with no named cause, because no
   kernel preflight names the btrfs driver (#3519)." with "the boot job ends `boot_timeout`.
   The spine preflight names `CONFIG_BTRFS_FS=y` only when its caller passes `root_fs="btrfs"`
   (#3580); no shipped caller does yet."
2. remote-live-stack.md: replace "On a kernel that has ext4, no preflight names a missing btrfs
   or XFS driver: the boot job ends `boot_timeout`." with "A kernel without the driver ends
   `boot_timeout`. The spine preflight names `CONFIG_BTRFS_FS=y` only when its caller passes
   `root_fs="btrfs"`; no shipped caller does yet, and the remote authority cell does not run it."
3. Run `just docs-links` and `just docs-paths`; expect exit 0. Commit `docs(live): state the btrfs preflight (#3580)`.
