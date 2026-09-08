# Historical KVM watchpoint proof (#922)

> **Historical record.** This preserves the original decision or dated evidence.
> Commands, status, paths and capabilities below describe that context; they are not
> current operating guidance. Start with the [current documentation](../README.md).

Record date: 2026-06-29. Decision: [ADR-0277](../adr/0277-gdb-watchpoints.md).

The #922 live exercise on real KVM completed the set/list/clear cycle. After
watching `jiffies`, `continue` stopped with `reason=watchpoint-trigger` in
`tick_do_update_jiffies64`.

This observation established a trap on the tested host. Trap reliability remains
kernel/QEMU/host-dependent; success at watchpoint creation alone does not prove
that a later write will trap. See the [debug guide](../guide/toolsets/debug.md)
for the current contract.
