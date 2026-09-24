# io_uring NOP file reference leak with IOSQE_FIXED_FILE

## Summary

- **Subsystem**: io_uring, `io_uring/nop.c`
- **Fix reference**: [2564ca2e31bd](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=2564ca2e31bd8ee8348362941af2ee4671e487ca) "io_uring/nop: fix file reference leak with IOSQE_FIXED_FILE"
- **Introduced by**: a85f31052bce "io_uring/nop: add support for testing registered files and buffers" (v6.13-rc1)
- **Fixed release**: Linux 7.2-rc1
- **Architecture**: generic
- **Primary symptom**: `struct file` objects leak, one per request
- **VM suitability**: Easy. Tiny io_uring program, deterministic.
- **KDIVE tools exercised**: `introspect.run`, `introspect.script`, `debug.set_breakpoint`, `debug.read_memory`, `systems.snapshot`, `systems.restore`, `runs.bind`, `runs.set`

## Bug description

A NOP request picks the fixed or normal file path from its own `IORING_NOP_FIXED_FILE` flag.
The generic `IOSQE_FIXED_FILE` flag sets `REQ_F_FIXED_FILE` independently. With
`IOSQE_FIXED_FILE` set and `IORING_NOP_FIXED_FILE` clear, `io_nop()` takes a real file
reference, but `io_put_file()` skips the release because `REQ_F_FIXED_FILE` is set.

## Suggested starting prompt

> A test program that submits io_uring NOP requests makes the open-file count grow without
> bound, even after the program exits. Find where the references are lost.

## Reproduction sketch

1. Open a file and set up an io_uring instance.
2. Submit N NOPs with `IORING_NOP_FILE` and `IOSQE_FIXED_FILE` set and `IORING_NOP_FIXED_FILE` clear.
3. Reap the completions and exit.
4. Compare `/proc/sys/fs/file-nr` or the `filp` slab count before and after. Snapshot the guest
   first so that each measurement starts from the same state.

## Expected signal on vulnerable kernel

- The file count grows by N and does not return.
- kmemleak reports objects from `alloc_empty_file()`.

## Fixed-kernel expectation

The file count returns to its start value after the program exits.

## A/B scoring hints

- Award credit for an exact count (N leaked for N requests).
- Award extra credit for showing the unequal hit counts of the get and put paths.
- Penalize a fix that changes `io_put_file()` for all opcodes.

## Caveats

Use the same System for the vulnerable and fixed Runs so the counts compare directly.
