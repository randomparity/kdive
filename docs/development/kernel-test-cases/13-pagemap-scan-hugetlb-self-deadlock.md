# PAGEMAP_SCAN write-protect on hugetlb self-deadlock

## Summary

- **Subsystem**: procfs, `fs/proc/task_mmu.c`; hugetlb and userfaultfd
- **Fix reference**: [e92d92bbafb2](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=e92d92bbafb264dc0518d52b846a3c07ed8d523f) "fs/proc/task_mmu: fix hugetlb self-deadlock in pagemap_scan_pte_hole()"
- **Introduced by**: 52526ca7fdb9 "fs/proc/task_mmu: implement IOCTL to get and optionally clear info about PTEs" (v6.7-rc1)
- **Fixed release**: Linux 7.2-rc1
- **Architecture**: generic
- **Primary symptom**: Calling thread hangs in D state and ignores SIGKILL
- **VM suitability**: Easy. Small C program, deterministic.
- **KDIVE tools exercised**: `control.diagnostic_sysrq`, `control.watch_for_crash`, `introspect.run`, `debug.interrupt`, `debug.backtrace`, `debug.read_frame`

## Bug description

`walk_hugetlb_range()` holds the hugetlb VMA lock for read across the walk. For a hole,
`pagemap_scan_pte_hole()` calls `uffd_wp_range()`, which reaches
`hugetlb_change_protection()` and takes the same lock for write. The thread waits in
`down_write()` for a read lock that it holds itself.

## Suggested starting prompt

> A program that write-protects a hugetlbfs range with the `PAGEMAP_SCAN` ioctl hangs forever,
> and `kill -9` has no effect. Explain what the thread waits for.

## Reproduction sketch

1. Map a hugetlbfs region and register it with userfaultfd in write-protect mode.
2. Leave part of the range unpopulated.
3. Call `ioctl(PAGEMAP_SCAN)` with `PM_SCAN_WP_MATCHING` over the whole range.
4. Collect task stacks with SysRq `w`/`t`; wait for the hung-task warning.

## Expected signal on vulnerable kernel

- The task is in D state in `down_write()` under `hugetlb_change_protection()`.
- The same task owns the VMA lock for read.

## Fixed-kernel expectation

The ioctl completes and installs the write-protect markers in the holes.

## A/B scoring hints

- Award credit for showing that the waiter and the lock holder are the same task.
- Award extra credit for naming the read-then-write nesting through `->pte_hole()`.
- Penalize if the agent reports a generic "hugetlb deadlock" without the lock owner.

## Caveats

Needs `CONFIG_HUGETLBFS`, `CONFIG_USERFAULTFD`, and reserved huge pages in the guest.
