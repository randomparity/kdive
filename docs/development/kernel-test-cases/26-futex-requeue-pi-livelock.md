# futex requeue-PI livelock on signal or timeout

## Summary

- **Subsystem**: futex (`kernel/futex/requeue.c`)
- **Fix reference**: [bc7304f3ae20](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=bc7304f3ae20972d11db6e0b1b541c63feda5f05) "futex: Prevent lockup in requeue-PI during signal/ timeout wakeup"
- **Introduced by**: 07d91ef510fb "futex: Prevent requeue_pi() lock nesting issue on RT" (v5.15-rc1)
- **Fixed release**: Linux 7.1-rc2
- **Architecture**: generic; easiest on a single-vCPU guest
- **Primary symptom**: One CPU spins in `futex_requeue()`; soft or hard lockup
- **VM suitability**: Hard. Race; best odds with 1 vCPU and real-time priorities.
- **KDIVE tools exercised**: `control.diagnostic_sysrq`, `control.watch_for_crash`, `debug.interrupt`, `debug.backtrace`, `debug.read_registers`, `debug.set_breakpoint`, `introspect.run`

## Bug description

A waiter in `futex_wait_requeue_pi()` times out, marks itself `Q_REQUEUE_PI_IGNORE`, and
blocks on the hash-bucket lock held by the requeuer. The requeuer sees the mark, drops both
locks, and retries at once. On one CPU, or when the requeuer has a higher priority, the waiter
never gets the lock and the requeuer spins forever.

## Suggested starting prompt

> A real-time application that uses condition variables with priority-inheritance mutexes
> sometimes locks up a single-CPU system. One CPU spins in the kernel. Explain the loop.

## Reproduction sketch

1. Boot the guest with 1 vCPU.
2. Run threads that wait with `pthread_cond_timedwait()` on a PI mutex with short timeouts,
   and a higher-priority real-time thread that broadcasts.
3. When the system stalls, collect the spinning CPU's stack with SysRq `l` and task stacks with `t`.
4. Interrupt the guest and read the requeuer's backtrace.

## Expected signal on vulnerable kernel

- A lockup report with `futex_requeue()` spinning.
- The waiter blocks on the hash-bucket lock with `requeue_state` set to ignore.

## Fixed-kernel expectation

The requeuer removes the leaving waiter and makes progress.

## A/B scoring hints

- Award credit for identifying a livelock, not a deadlock.
- Award extra credit for naming the `-EAGAIN` retry loop.
- Penalize if the agent blames the scheduler.

## Caveats

This case is a race. Record the number of attempts and the time to the first stall.
