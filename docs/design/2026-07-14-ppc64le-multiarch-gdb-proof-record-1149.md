# Proof record — cross-arch gdb attach to a ppc64le gdbstub (#1149)

Date: 2026-07-14
Issue: #1149 · Epic: #1139 · Spec: `2026-07-14-ppc64le-multiarch-gdb-1149.md` · ADR-0347

This is the documented live proof required by #1149 AC5: a gdb attaches to a **ppc64le** guest's
gdbstub from the **x86_64** host and reads registers, through the real arch-aware
`GdbMiEngine.attach` path this PR adds.

## Environment

- Host: x86_64 (Fedora). Plain `/usr/bin/gdb` present and **multiarch-built** (no `gdb-multiarch`
  package on Fedora), so `set architecture powerpc:common64` succeeds. `qemu-system-ppc64` present.
- Target: a paused `pseries`/TCG guest exposing a gdbstub on `127.0.0.1:1234`, booted from the
  #1146 bundle's baseline ppc64le kernel
  (`/home/dave/kdive-ppc-proof/bundle/vmlinuz-6.19.10-300.fc44.ppc64le`, an
  `ELF 64-bit LSB executable, 64-bit PowerPC, OpenPOWER ELF V2 ABI` — powerpc has no bzImage; the
  bootable image is an ELF `vmlinux`).

  ```
  qemu-system-ppc64 -machine pseries -accel tcg -m 1024 \
    -kernel .../vmlinuz-6.19.10-300.fc44.ppc64le -append "console=hvc0" \
    -S -gdb tcp:127.0.0.1:1234 -display none -serial null -monitor none
  ```

## Method

Drove the **real** `GdbMiEngine.attach()` (the same code the Debug plane runs; `live_vm`-only, so
not unit-covered) against the gdbstub with the guest's ppc64le ELF as the symbol file, then read
the register set. This exercises the whole new path over real RSP — `arch_from_elf` →
`select_gdb_binary` → the cross-arch `-gdb-set architecture` → `-target-select remote` →
`read_registers` — without the full MCP stack, and without disturbing the host's running
`live_stack`. Driver: `scratchpad/live_attach_proof.py`.

## Result — PASS

```
arch_from_elf -> 'ppc64le'
select_gdb_binary(x86_64, ppc64le) -> '/usr/bin/gdb'      # Fedora multiarch fallback (no gdb-multiarch)
live register-name count: 366
ppc64le regs present: ['ctr', 'lr', 'pc', 'r0', 'r1', 'r31']
x86 regs present:     []
read_registers(r1,pc): {'r1': '0x0', 'pc': '0x1000000000000'}

AC5 PROOF PASS: gdb attached to a ppc64le gdbstub and read ppc64le registers.
```

Proves, over real RSP from the x86_64 host to a ppc64le target:

- **Guest arch derived from the ELF** — `arch_from_elf` returned `ppc64le` from the real vmlinux
  `e_machine`, driving the cross-arch branch.
- **Binary selection** — with no `gdb-multiarch`, `select_gdb_binary` fell back to `/usr/bin/gdb`
  (the multiarch build), the documented Fedora path (ADR-0347).
- **Arch is correct, not a misread** — the live `-data-list-register-names` set is the ppc64le
  register file (`r0/r1/r31`, `pc`, `lr`, `ctr` present) with **no** x86 names (`rax`/`rip`/`rsp`),
  the arch-discriminating signal AC5 requires. `read_registers` returned real values (`pc =
  0x1000000000000`, the pseries reset entry).

## Scope / notes

- The proof attaches to a paused (reset-state) pseries gdbstub; reading registers needs no running
  userspace or SSH, so it isolates the Debug-plane attach path from the boot/readiness path already
  live-proven in #1144. A full provision→boot→`debug.start_session`→attach through the MCP stack
  rides the identical `attach` code; the only arch-specific steps are the ones proven here.
- The RSP transport and MI register layer are unchanged by this PR (ADR-0034, arch-neutral); this
  PR adds only host-side binary selection + the explicit target arch, both exercised above.
