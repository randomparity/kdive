# taprio class dump NULL dereference after child qdisc delete

## Summary

- **Subsystem**: networking, traffic control (`sch_taprio`)
- **Fix reference**: [3d07ca5c0fae](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=3d07ca5c0fae311226f737963984bd94bb159a87) "net/sched: taprio: fix NULL pointer dereference in class dump"
- **Introduced by**: 665338b2a7a0 "net/sched: taprio: dump class stats for the actual q->qdiscs[]" (v6.6-rc1)
- **Fixed release**: Linux 7.1-rc2
- **Architecture**: generic
- **Primary symptom**: Oops in `taprio_dump_class()` on a class dump
- **VM suitability**: Easy. Four `tc` commands, deterministic.
- **KDIVE tools exercised**: `control.watch_for_crash`, `vmcore.fetch`, `postmortem.crash`, `introspect.from_vmcore`, `debug.set_breakpoint`, `debug.set_watchpoint`, `debug.load_module_symbols`

## Bug description

Deleting a taprio child qdisc calls `taprio_graft()` with `new == NULL`, which stores NULL in
`q->qdiscs[cl - 1]`. A later class dump walks every class, `taprio_leaf()` returns that NULL,
and `taprio_dump_class()` reads `child->handle`. With unprivileged user namespaces, an
unprivileged user can reach this path in a new network namespace.

## Suggested starting prompt

> After a child qdisc under taprio is deleted, `tc class show` on the device crashes the kernel.
> Find the NULL pointer and explain how the qdisc tree reached that state.

## Reproduction sketch

1. Create a network device and install a taprio root qdisc (`tc qdisc replace ... taprio ...`).
2. Graft an explicit child qdisc onto one taprio class.
3. Delete that child qdisc.
4. Run `tc class show dev <dev>`. Set `panic_on_oops=1` to get a vmcore.

## Expected signal on vulnerable kernel

- Oops or general protection fault in `taprio_dump_class()`.
- The crashed kernel shows a NULL slot in `q->qdiscs[]` for the deleted class.

## Fixed-kernel expectation

The class dump succeeds; the deleted slot holds `noop_qdisc`, not NULL.

## A/B scoring hints

- Award credit for finding the NULL slot in the vmcore, not only the faulting line.
- Award extra credit for a watchpoint or breakpoint that shows `taprio_graft()` storing NULL.
- Penalize a fix that adds a NULL check only in the dump path without explaining the graft.

## Caveats

`sch_taprio` is usually a module; load its symbols before setting breakpoints.
