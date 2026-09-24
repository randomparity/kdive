# procfs parent directory nlink leak

## Summary

- **Subsystem**: procfs, `fs/proc/generic.c`
- **Fix reference**: [16b02eb4b9b2](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=16b02eb4b9b272c221255c20d34ccd5db53a3ed3) "proc: only bump parent nlink when registering directories"
- **Introduced by**: e06689bf5701 "proc: change ->nlink under proc_subdir_lock" (v5.5-rc1)
- **Fixed release**: Linux 7.2-rc2
- **Architecture**: generic
- **Primary symptom**: Wrong and growing link count on `/proc` directories
- **VM suitability**: Easy. Visible at boot, deterministic.
- **KDIVE tools exercised**: `introspect.script`, `introspect.run`, `debug.set_watchpoint`, `debug.set_breakpoint`, `debug.backtrace`

## Bug description

`proc_register()` increments the parent's `nlink` for every entry, but the remove paths
decrement it only for directories. Regular files inflate the parent's count while they exist
and leak one link on every create and remove cycle.

## Suggested starting prompt

> `stat` on `/proc/bus/pci/00` reports a link count much larger than 2, although the directory
> has no subdirectories, and the count grows over time. Explain the accounting error.

## Reproduction sketch

1. Run `stat -c %h /proc/bus/pci/<bus>` and compare it with the subdirectory count.
2. Loop a create and remove of a regular `/proc` entry (for example, load and unload a module
   that creates one) and watch the count climb.
3. Walk the `proc_dir_entry` tree and compare each directory's `nlink` with its real
   subdirectory count.

## Expected signal on vulnerable kernel

- A directory holding regular files reports `2 + <file count>` or more.
- The value grows by one per create and remove cycle.

## Fixed-kernel expectation

Directory link counts equal 2 plus the number of subdirectories and do not drift.

## A/B scoring hints

- Award credit for a tree walk that finds every affected directory.
- Award extra credit for a watchpoint that catches a spurious increment.
- Penalize a fix that only decrements on removal of regular files.

## Caveats

No crash, and nothing to clean up. The case tests inspection tools only.
