# hugetlb boot parameter without `=` NULL dereference

## Summary

- **Subsystem**: mm, hugetlb command-line parsing
- **Fix reference**: [c45b354911d0](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=c45b354911d01565156e38d7f6bc07edb51fc34c) "mm/hugetlb: fix early boot crash on parameters without '=' separator"
- **Introduced by**: 5b47c02967ab "mm/hugetlb: convert cmdline parameters from setup to early" (v6.15-rc1)
- **Fixed release**: Linux 7.1-rc1
- **Architecture**: generic
- **Primary symptom**: NULL pointer dereference in `hugetlb_add_param()` during early boot
- **VM suitability**: Easy. One boot argument, deterministic.
- **KDIVE tools exercised**: `debug.start_session`, `debug.set_breakpoint`, `debug.read_registers`, `debug.disassemble`, `debug.resolve_symbol`, `debug.continue`, `control.power`, `control.watch_for_crash`

## Bug description

The `hugepages`, `hugepagesz`, and `default_hugepagesz` early parameters pass their value
to `hugetlb_add_param()`. When the parameter has no `=`, the value is NULL, and the function
calls `strlen()` on it before any validation. The kernel faults before the console is fully up.

## Suggested starting prompt

> A kernel booted with a bare `hugepages` argument (no value) never reaches userspace. The
> console shows at most a short early-boot oops. Find the faulting function and explain why this
> argument form crashes the kernel.

## Reproduction sketch

1. Boot the vulnerable 7.0 kernel with `hugepages` (no `=`) on the command line.
2. Attach a debug session before the guest runs and set a breakpoint on `hugetlb_add_param`.
3. Read the argument register at the breakpoint and step to the `strlen()` fault.
4. Boot the same kernel without the argument, then the fixed kernel with it.

## Expected signal on vulnerable kernel

- Early-boot oops with the program counter in `strlen()` called from `hugetlb_add_param()`.
- The value argument is NULL at the breakpoint.

## Fixed-kernel expectation

The kernel rejects the parameter with `-EINVAL` and boots normally.

## A/B scoring hints

- Award credit if the agent connects the crash to early parameter parsing, not to hugetlb pool setup.
- Award extra credit if it shows the NULL value in a register before the fault.
- Penalize if it only reports "boot failure" without a faulting function.

## Caveats

The fault happens before most console output, so the case depends on the debug session. Live
gdbstub debug is not yet proven on ppc64le guests (tracked by #2736); run it on an x86_64 host.
