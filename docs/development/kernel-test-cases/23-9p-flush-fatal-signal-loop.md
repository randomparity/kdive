# 9p client unkillable loop on a fatal signal

## Summary

- **Subsystem**: 9p client (`net/9p/client.c`)
- **Fix reference**: [6b4f48728faa](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=6b4f48728faa8bb514368f7eacda05565dea8696) "net/9p: fix infinite loop in p9_client_rpc on fatal signal"
- **Introduced by**: 91b8534fa8f5 "9p: make rpc code common and rework flush code" (v2.6.28-rc1)
- **Fixed release**: Linux 7.2-rc1
- **Architecture**: generic
- **Primary symptom**: Task stays in D state after SIGKILL; a coredump never finishes
- **VM suitability**: Medium. Deterministic once a peer-less 9p transport is set up.
- **KDIVE tools exercised**: `control.diagnostic_sysrq`, `debug.interrupt`, `debug.backtrace`, `debug.set_breakpoint`, `debug.read_registers`, `debug.load_module_symbols`, `introspect.run`

## Bug description

In the `P9_TFLUSH` retry path, `p9_client_rpc()` clears `TIF_SIGPENDING` and waits again.
The wait then sees no pending signal and sleeps. Each new signal repeats the cycle, so with no
9p peer the task never returns. A multi-threaded coredump that kills such a thread waits
forever.

## Suggested starting prompt

> A process that uses a 9p mount over an `fd` transport cannot be killed after the server goes
> away, and a crash of one of its threads never produces a core. Explain why SIGKILL does not
> end the task.

## Reproduction sketch

1. Mount 9p over the `fd` transport with pipes and no server on the other end.
2. Start a 9p operation in one thread, then send SIGKILL (or crash a sibling thread).
3. Collect task stacks with SysRq `t`; wait for the hung-task warning.
4. Break in `p9_client_rpc` and read the task's thread flags on each pass.

## Expected signal on vulnerable kernel

- The task is in D state with `p9_client_rpc()` on the stack.
- `TIF_SIGPENDING` is clear although SIGKILL is pending.

## Fixed-kernel expectation

The task returns `-ERESTARTSYS` and exits on SIGKILL.

## A/B scoring hints

- Award credit for finding the cleared signal flag.
- Award extra credit for linking the hang to the coredump wait.
- Penalize if the agent blames the transport alone.

## Caveats

9p is usually built as modules; load their symbols before setting breakpoints.
