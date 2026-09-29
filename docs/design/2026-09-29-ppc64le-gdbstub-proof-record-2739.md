# Proof record — native POWER pseries gdbstub (#2739)

> Historical proof or design for the dated checkout below, not current operating instructions.
> Use the [current documentation index](../README.md) for supported workflows.

Date: 2026-09-29
Issue: #2739 · Tracker: #2736 · Consumer: #2740 · Spec:
[`../workflow/specs/2026-09-29-ppc64le-gdbstub-proof-design.md`](../workflow/specs/2026-09-29-ppc64le-gdbstub-proof-design.md)

This record shows which parts of the pseries gdbstub debug path work on a native ppc64le
(POWER9) KVM-HV host, and which runtime and test changes #2740 needs. The earlier ppc64le
proof ([#1149](2026-07-14-ppc64le-multiarch-gdb-proof-record-1149.md)) used TCG from an x86_64
host. This one uses hardware virtualization and native gdb.

## Summary

| Part | Result |
|------|--------|
| gdbstub transport (`-gdb tcp:127.0.0.1:<port>`) under KVM-HV | works |
| gdb target selection without `set architecture` | works: gdb picks `powerpc:common64` from the vmlinux |
| register names | `pc`, `msr`, `lr`, `ctr`, `r0`-`r31` exist; `nip` and `rip` do not |
| software breakpoints on a booted kernel | work |
| `finish`, `advance`, `tbreak` | work |
| `stepi`, `nexti`, `step`, `next`, `until` | the vCPU does not stop again (no single-step) |
| hardware breakpoints and watchpoints | refused by the stub |
| attach at reset (`-S`) | connects, but the registers read byte-swapped and kernel breakpoints never fire |
| attach to a halted early-boot panic (ADR-0233) | works: the proof passes |
| three native proofs, arch gate widened | 2 pass unchanged; advance-modes fails (register name, then single-step) |

## Environment

- Host `sys-P1`: POWER9 (`cpu: POWER9, altivec supported`), Ubuntu 26.04.1 LTS, kernel
  `7.0.0-31-generic`, `kvm_hv` loaded, `/dev/kvm` present.
- Tools: `gdb 17.1-2ubuntu1` (plain `/usr/bin/gdb`, no `gdb-multiarch`), `qemu-system-ppc
  1:10.2.1+ds-1ubuntu3.2` (SLOF `release 20251026`), `libvirt-daemon 12.0.0-1ubuntu5.5`.
- Guest kernel: Linux 7.0.1 (`v7.0.1`, tree not dirty) built on the host,
  `<REDACTED-HOME>/src/linux/vmlinux`, ELF 64-bit LSB, OpenPOWER ELF V2 ABI, DWARF5, Build ID
  `55ba224a2dc0676cc02eb75a7d56e7bbe88e6775`. `CONFIG_RELOCATABLE=y`, `CONFIG_KGDB` not set.
- kdive checkout: `af2f83034` (origin/main at spike time), in a detached worktree. No source
  change was committed; section 5 lists the host-only test edits.

## Method

Steps 1-3 start `qemu-system-ppc64` directly, one fresh guest per gdb session, because a gdb
detach resumes the guest:

```
qemu-system-ppc64 -name kdive-2739-spike-<n> -machine pseries,accel=kvm -cpu host -m 2048 \
  -smp 2 -display none -monitor none -serial file:<proof-dir>/console-<n>.log \
  -kernel <REDACTED-HOME>/src/linux/vmlinux -append "console=hvc0 nokaslr" -S \
  -gdb tcp:127.0.0.1:<port> </dev/null &
```

For a booted kernel, the guest runs without `-S` and with `root=/dev/vda rootwait` and no disk.
The kernel then loops in `wait_for_root()` (`msleep(5)`) and does not panic. Every gdb session
runs as `timeout <N> gdb -nx -batch ... <vmlinux>`; exit status 124 means the timeout fired.

Harness quirk, not a kdive result: a directly started guest with a file serial console stayed
in SLOF at `Press "s" to enter Open Firmware.` for 40-60 s with `-smp 1` and `-smp 2`. One
monitor `stop`/`cont` (or a gdb attach and `continue`) let it boot in about 8 s. The
libvirt-rendered kdive domains in step 5 booted without this.

## 1. Transport and target selection

Guest 1, no `set architecture`:

```
timeout 120 gdb -nx -batch -ex 'show architecture' -ex 'target remote 127.0.0.1:51391' \
  -ex 'show architecture' -ex 'info registers pc' -ex 'maint print registers' \
  -ex 'p $pc' -ex 'p $r1' -ex 'p $nip' -ex 'p $rip' -ex 'p $msr' -ex 'p $lr' <vmlinux>
exit 0
The target architecture is set to "auto" (currently "powerpc:common64").
0x0001000000000000 in ?? ()
pc             0x1000000000000     0x1000000000000
```

Guest 2 runs the same with `-ex 'set architecture powerpc:common64'` first: exit 0, the same
architecture, and a `maint print registers` table identical to guest 1 (`diff` empty).

**Result:** native gdb connects to the pseries KVM-HV gdbstub. The architecture comes from the
vmlinux ELF, so `set architecture powerpc:common64` is not needed on a native attach. This
agrees with `GdbMiEngine.attach`, which sets it only on the cross-arch path.

## 2. Register names

From `maint print registers` in guest 1 and the engine's `-data-list-register-names` (step 4):

- 578 register slots, 357 with a name. Present: `pc` (Nr 64), `msr` (65), `cr`, `lr` (67),
  `ctr` (68), `xer`, `r0`-`r31`, `f0`-`f31`, `fpscr`, `vscr`, `vrsave`.
- `p $nip` and `p $rip` print `void`: gdb has no register with either name.
- Names that occur twice: `ctr`, `lr`, `tbu`, `vrsave`, `xer` (a second copy at Nr 326-356 from
  the stub's system-register block). `read_registers` maps a name to its first ordinal. For `lr`
  it returned `0xc000000001597718`, the return address in `check_and_cede_processor` that raw
  gdb also showed at the same idle stop in guest 12. The second copy was not cross-checked.

**Result:** the POWER program counter is `pc`. The name `nip` in `tests/integration/test_live_stack.py`
(`_SPINE_PC_REGISTER`) does not exist in gdb.

## 3. Early boot and execution control

**Attach at reset (`-S`).** gdb follows the little-endian vmlinux, but at reset the vCPU is in
big-endian SLOF firmware. The stub sends registers in the vCPU's current byte order, so gdb reads
them byte-swapped:

```
The target endianness is set automatically (currently little endian).
pc             0x1000000000000     0x1000000000000
msr            0x30000000000000    13510798882111488
The target is set to big endian.            # after: set endian big
pc             0x100               0x100
msr            0x3000              12288
```

The same holds for a vCPU that is still in SLOF after boot starts (guest 7: `pc
0x9c25af7d00000000` little-endian, `NIP 000000007daf259c` from the monitor's `info registers`).
The #1149 record's `pc = 0x1000000000000` is this byte-swapped `0x100`.

Kernel breakpoints set at reset never fire. Guest 4:

```
timeout 120 gdb -nx -batch -ex 'target remote 127.0.0.1:51394' -ex 'break start_kernel' \
  -ex 'break early_setup' -ex 'break mount_root_generic' -ex 'continue' <vmlinux>
```

No breakpoint stopped the guest. gdb printed `[Inferior 1 (process 1) exited normally]` when the
kernel panicked (no root device) and QEMU exited; gdb exit 0. Guest 14 shows why, with
`-ex 'x/4xw start_kernel' -ex 'x/4xw (char *)start_kernel + 0x400000'` at the reset halt:

```
0xc000000002003d5c <start_kernel>:      0x00000000  0x00000000  0x00000000  0x00000000
0xc000000002403d5c:                     0x3c4cffba  0x384243a4  0x60000000  0x3d2200b1
```

At reset the kernel image sits 0x400000 above its link address; the vmlinux file has
`0x3c4cffba 0x384243a4 ...` at `start_kernel`. On a booted kernel the code is at its link
address (guest 12 disassembles `msleep` there), so the kernel moves itself during boot and
overwrites a breakpoint planted at reset. `-machine pseries,kernel-addr=0` is refused: `Some ROM
regions are overlapping` (`slof.bin` at 0x0-0xf3808).

**Attach to a booted kernel.** Guest 12 attached after the console showed `Waiting for root
device /dev/vda...`:

```
* 1    Thread 1.1 (CPU#0 [running]) plpar_hcall_norets_notrace () at .../hvCall.S:114
pc             0xc0000000001b31b4  0xc0000000001b31b4 <plpar_hcall_norets_notrace+24>
msr            0x8000000002109033  9223372036889415731
#1  0xc000000001597718 in cede_processor () at ./arch/powerpc/include/asm/plpar_wrappers.h:27
Thread 1 hit Breakpoint 1, 0xc00000000034aef8 in msleep (msecs=msecs@entry=5) at .../sleep_timeout.c:314
#1  0xc0000000020059c8 in wait_for_root (root_device_name=... "/dev/vda") at init/do_mounts.c:420
```

Guest 13 probed each command in its own gdb session on one booted guest; `<probe>` is the
command under test, and a `continue` follows the insert-type probes:

```
timeout 25 gdb -nx -batch -ex 'target remote 127.0.0.1:51403' -ex 'break msleep' \
  -ex 'continue' -ex 'delete' -ex '<probe>' -ex 'info registers pc' <vmlinux>
```

| Command | Exit | Observed |
|---------|------|----------|
| `stepi`, `nexti`, `step`, `next`, `until` | 124 | the vCPU resumed and did not stop again; the next command printed `Cannot execute this command while the target is running.` |
| `finish` | 0 | stopped in `wait_for_root` at `init/do_mounts.c:420` |
| `advance schedule_timeout` | 0 | stopped at `schedule_timeout+8` |
| `tbreak schedule_timeout`, `continue` | 0 | `Thread 1 hit Temporary breakpoint 2` |
| `hbreak schedule_timeout`, `continue` | 0 | `Cannot insert hardware breakpoint 2:Remote failure reply: 22.` |
| `watch jiffies` / `rwatch jiffies`, `continue` | 0 | `Could not insert hardware watchpoint 2.` |

**Result:** software breakpoints and breakpoint-based commands work on a booted kernel. There is
no single-step and no hardware breakpoint or watchpoint on this KVM-HV host. An attach at reset
reaches only firmware state; kdive's early-boot path is the ADR-0233 panic halt (step 5).

## 4. Real engine

A driver in the kdive worktree (`uv run python engine_drive.py <port> <vmlinux> <transcript>`,
exit 0) called the Debug-plane engine against guest 16, a booted kernel. Its call sequence, each
call in its own `try` that prints the result or the exception and its `details`:

```python
eng = GdbMiEngine()
att = eng.attach(host="127.0.0.1", port=port, vmlinux_path=vmlinux, transcript_path=transcript)
register_names(eng.execute_mi_command(att, "-data-list-register-names"))
eng.read_registers(att, ["pc", "r1", "msr", "lr"]); eng.read_registers(att, ["nip"])
eng.read_registers(att, ["rip"])
bp = eng.set_breakpoint(att, "msleep"); eng.continue_(att, timeout_sec=20)
eng.read_registers(att, ["pc"]); eng.clear_breakpoint(att, bp.number)
eng.step_instruction(att, timeout_sec=10); eng.read_registers(att, ["pc"])
bp = eng.set_breakpoint(att, "msleep"); eng.continue_(att, timeout_sec=20)
eng.clear_breakpoint(att, bp.number)
eng.next(att, timeout_sec=10); eng.finish(att, timeout_sec=10); eng.interrupt(att)
```

Output:

```
host arch: ppc64le | arch_from_elf: ppc64le | gdb: /usr/bin/gdb
attach: ok
register names: 578 | named: 357 | duplicates: ['ctr', 'lr', 'tbu', 'vrsave', 'xer']
read_registers[pc,r1,msr,lr]: {'pc': '0xc0000000001b31b4', 'r1': '0xc0000000029e7cb0', ...}
read_registers[nip]: CategorizedError: gdb/MI omitted requested register data
    details={'code': 'missing_registers', 'requested': ['nip'], 'missing': ['nip']}
read_registers[rip]: CategorizedError: ... 'missing': ['rip']
set_breakpoint msleep: GdbBreakpointRef(number='1', ..., func='msleep', ...)
continue_: GdbStopRecord(reason='breakpoint-hit', ..., func='msleep', ..., timed_out=False)
step_instruction: CategorizedError: gdb/MI RSP went silent: interrupt issued but no *stopped
    arrived; link stalled details={'code': 'transport_stall', ...} (21.5s)
next: CategorizedError: ... details={'code': 'transport_stall', 'verb': '-exec-next'} (21.5s)
finish: GdbStopRecord(reason=None, ..., func='cede_processor', ...)
interrupt: None (10.7s)
```

`finish` here ran after the stalled `next`, so its stop (no reason, in the idle loop) is not a
clean `finish` result; step 3 has the clean one.

## 5. The three native proofs

In the kdive worktree, `_GDBSTUB_PROVEN_ARCHES` was widened to `{"x86_64", "ppc64le"}` (host
only, restored by an exit trap; `git status` empty afterwards). Environment:
`KDIVE_LIBVIRT_URI=qemu:///session`, `KDIVE_LIVE_VM_BZIMAGE` and `KDIVE_LIVE_VM_VMLINUX` = the
7.0.1 vmlinux (pseries boots the ELF; there is no bzImage), `KDIVE_LIVE_VM_ROOTFS` = the Fedora
44 ppc64le fixture image, `KDIVE_REQUIRE_DOCKER=1`. Each proof ran alone with
`timeout 900 uv run python -m pytest -m live_vm <node-id> -p no:randomly -q -rA --tb=short`.

| Proof | Exit | Result |
|-------|------|--------|
| `test_debug_live_attach.py::test_live_vm_start_session_attaches_to_halted_early_boot_crash` | 0 | `1 passed in 19.24s` |
| `test_debug_gdbmi_live_smoke.py::test_live_vm_gdbmi_promoted_ops_smoke` | 0 | `1 passed in 22.25s` |
| `test_debug_gdbmi_live_smoke.py::test_live_vm_debug_advance_modes` | 1 | `missing_registers` on `rip` (below) |

The advance proof booted the guest, reached SSH, and hit its `vfs_read` breakpoint. It then
failed in `_read_instruction_pointer`:

```
E   AssertionError: ToolResponse(..., status='error', ..., detail='gdb/MI omitted requested
    register data', data={'code': 'missing_registers'}, items=[])
```

One more run replaced `rip` with `pc` in that helper (host only, restored). The first mode,
`into`, then failed:

```
tests/mcp/debug/test_debug_gdbmi_live_smoke.py:556: in _assert_nonterminal_stop
E   AssertionError: ToolResponse(..., status='stopped', ..., data={'timed_out': True,
    'reason': 'signal-received'}, items=[])
```

These edits are evidence for #2740. They do not show that the advance proof can pass. Not
observed: the `over`, `instruction`, and `out` modes through `debug.advance`, and a smoke-proof
watchpoint armed across a resume (the smoke proof sets and clears it on a halted guest, and gdb
inserts watchpoints only on resume).

## Changes #2740 needs

| # | Change | Evidence |
|---|--------|----------|
| 1 | Read the program counter as `pc` on ppc64le in the advance proof (`_read_instruction_pointer` reads `rip`). | `tests/mcp/debug/test_debug_gdbmi_live_smoke.py` `_read_instruction_pointer`; sections 2, 5 |
| 2 | In the live-stack spine: change `_SPINE_PC_REGISTER["ppc64le"]` from `nip` to `pc`, the `("ppc64le", "nip")` parameter of `test_spine_gdbstub_profile_and_register`, the two `gdbstub=arch == "x86_64"` expressions, and the `if arch == "x86_64":` attach branch that records `_SPINE_GDBSTUB_GAP`. This spike did not run the spine on POWER, so its ppc64le attach is unobserved. | `tests/integration/test_live_stack.py` `_SPINE_PC_REGISTER`, `test_spine_gdbstub_profile_and_register`, the `attach` phase; section 2 |
| 3 | Decide how `debug.advance` modes `into`, `over`, and `instruction` behave on ppc64le KVM-HV. The vCPU does not single-step: the MCP path returns `timed_out: True`, and the engine path returns `transport_stall`, which names a transport fault rather than an unsupported operation. `out` (`finish`) works in raw gdb. | `src/kdive/providers/shared/debug_common/gdbmi/core/engine.py` `step`, `next`, `step_instruction`; sections 3, 4, 5 |
| 4 | Decide how `debug.set_watchpoint` behaves on ppc64le KVM-HV: the stub cannot insert a hardware watchpoint, so a resume with one armed fails. Breakpoints need no change: `set_breakpoint` already uses software breakpoints (#711). | `src/kdive/providers/shared/debug_common/gdbmi/commands/watchpoints.py`, `commands/breakpoints.py`; section 3 |
| 5 | Add `ppc64le` to `_GDBSTUB_PROVEN_ARCHES` once 1 and 3 hold; 2 of 3 proofs already pass with only this change. | `tests/mcp/debug/session_support.py`; section 5 |

No change is needed for gdbstub transport, gdb binary selection, or target selection:
`select_gdb_binary` returned `/usr/bin/gdb`, and a native attach needs no `set architecture`
(sections 1, 4). An attach at reset is not a usable kernel debug path on pseries (section 3). The
early-boot panic attach from ADR-0233 already works.

## Cleanup

Each direct guest was killed by its script's exit trap. After the spike, `virsh -c
qemu:///session list --all --name` matched the empty list taken before it, no
`qemu-system-ppc64` process remained, and the kdive worktree had no changes.
