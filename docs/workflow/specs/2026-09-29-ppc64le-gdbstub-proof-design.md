# Native POWER pseries gdbstub proof — design

Issue: #2739 (part of #2736; findings consumed by #2740). Scope token: q2739-e09b9bb2. No ADR:
the change records evidence and picks no policy.

## Problem

The native live gdbstub debug proofs skip on ppc64le (`tests/mcp/debug/session_support.py:61-79`,
`_GDBSTUB_PROVEN_ARCHES = {"x86_64"}`). The live-stack spine provisions ppc64le without a gdbstub
(`tests/integration/test_live_stack.py:224-234,448`) and names `nip` as the ppc64le program
counter. The advance-modes proof reads `rip` (`tests/mcp/debug/test_debug_gdbmi_live_smoke.py`,
`_read_instruction_pointer`). `GdbMiEngine.attach` sets `-gdb-set architecture` only on the
cross-arch path (`src/kdive/providers/shared/debug_common/gdbmi/core/engine.py`, `attach`), so a
native POWER attach relies on gdb to infer the architecture. The only earlier ppc64le gdbstub
proof (#1149) used TCG from an x86_64 host and a paused reset-state guest. Nobody has shown
what works under native KVM-HV, so #2740 has no evidence to size its runtime and test changes.

## Scope

One proof record, `docs/design/2026-09-29-ppc64le-gdbstub-proof-record-2739.md`, in the format
of `docs/design/2026-07-14-ppc64le-multiarch-gdb-proof-record-1149.md`. It holds the results of
four spike steps, run on a native ppc64le (POWER9) KVM-HV host:

1. **Transport and target selection (criteria 1, 2).** Start a pseries KVM-HV guest halted at
   reset (`-S`) with `-gdb tcp:127.0.0.1:<port>`, from the host's 7.0.1 kernel tree `vmlinux`.
   Attach native `/usr/bin/gdb` with that `vmlinux` and no `set architecture`. Record
   `show architecture` and the connect result. Repeat with `set architecture powerpc:common64`.
2. **Register names (criterion 3).** Record the gdb register-name list and whether `pc`, `nip`,
   `rip`, `r1`, `lr`, `msr` exist. Read `pc` and `r1`.
3. **Early-boot attach and execution control (criterion 4).** From the reset halt, set a
   software breakpoint on `start_kernel`, continue, and record the stop. Then record `stepi`,
   `next`, `finish`, and `hbreak` results, because `debug.advance` and the smoke proof use them.
4. **Real engine and the three native proofs (criterion 5).** Drive `GdbMiEngine.attach` and
   `read_registers` from a host checkout at the tested SHA. Then run the three gated proofs
   (`test_live_vm_gdbmi_promoted_ops_smoke`, `test_live_vm_debug_advance_modes`,
   `test_live_vm_start_session_attaches_to_halted_early_boot_crash`) with the arch gate widened
   by an uncommitted host-only edit. Record the first failure of each and its cause. Where the
   first failure is an x86 register name, repeat once with a host-only register-name edit.

The record closes with a findings table: each runtime or test change #2740 needs, with file
evidence. No source, test, or runtime file changes in this PR (exclusion: fixes -> #2740).
Each step records the exact command, exit code, tool versions, the result, and the failure
boundary. A step that cannot run records why and what it blocks. Each gdb session in steps 1-3
and the engine drive uses a fresh guest halted at reset, because a gdb detach resumes the
guest. A skip or fixture failure in step 4 is an environment result, not a #2740 change.

## Failure model

1. **Actors and deployments** — the campaign worker on a shared native POWER9 KVM-HV validation
   host; readers of the committed proof record (public repository).
2. **Invariants and assets at stake** — the host's other work: its shared kdive checkout,
   other proof directories, and the running stack stay untouched; every spike guest and
   domain is destroyed. The record publishes no host name, non-loopback IP, user name, home
   path, or credential such as a database URL (`sys-P1`, `<REDACTED-HOME>`, `<REDACTED-USER>`,
   `<REDACTED-DSN>` tokens).
3. **Accepted failure classes** — a step that fails on the host is a result, not a defect of
   this change, and is recorded as a finding for #2740. A proof run on this host's Fedora 44
   guest image and 7.0.1 kernel does not show behavior for other kernels or distributions;
   the record states its environment.
4. **Covered elsewhere** — runtime and test fixes: #2740. x86 behavior: operator. TCG
   cross-arch gdb: #1149 record and operator. Remote-libvirt gdbstub: operator.

## Success

- The record answers criteria 1-5 of `WORK:SCOPE` q2739-e09b9bb2, each with a command and an
  observed result.
- The findings table names each change observed up to each proof's last recorded failure, plus
  those found by reading the code, and states for each proof what stays unobserved past it.
- `just docs-links`, `just docs-paths`, and `just records` pass; the plan's PII `rg` check of
  the record finds no match.

## Validation

- Contract: the proof-record document. Mode: `task-test-not-applicable`. Reason: the record
  is a human-readable evidence report with no executable consumer; its correctness is the
  observed host output it quotes, which no repository test can reproduce off the host.
- Contract: repository doc links and paths. Mode: covered by `just docs-links` and
  `just docs-paths` (existing gates).
