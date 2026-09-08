# Spec: Complete the gdb-MI debug plane with stepping (#1255)

> **Historical record.** This preserves the original decision or dated evidence.
> Commands, status, paths and capabilities below describe that context; they are not
> current operating guidance. Start with the [current documentation](../../README.md).

- Issue: #1255 "Complete Debug Tool Features"
- ADR: [ADR-0379](../../adr/0379-gdb-source-and-instruction-stepping.md)
- Status: Design accepted

## KVM observation — 2026-07-17

The original KVM experiment compared an executing PC with a panic-halted CPU.
At an executing PC, booted paused at the entry vector, `step_instruction` advanced
`rip` cleanly across five steps; at a `hlt`-parked panic it stalled in the transport
(`INFRASTRUCTURE_FAILURE`). In a region without symbols, `step` and `next` returned
`DEBUG_ATTACH_FAILURE`. A panic-halted smoke therefore did not prove stepping:
its CPU was parked in the non-returning panic path.

This records the live observation, not a current script invocation or proof that
all four verbs completed. The original no-hang mechanism was tested separately
with a deterministic fake.
