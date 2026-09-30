# ppc64le live gdbstub tests (#2740)

## Problem

The native gdbstub proofs skip on ppc64le because `_GDBSTUB_PROVEN_ARCHES` holds only
`x86_64`. The [#2739 proof record](../../design/2026-09-29-ppc64le-gdbstub-proof-record-2739.md)
shows that POWER gdb names the program counter `pc` (not `rip` or `nip`), and that the KVM-HV
vCPU does not single-step. Part of #2736.

## Scope

Tests only; no `src/` change and no ownership change. The single-step runtime contract is #2942.

- `tests/mcp/debug/session_support.py`: add `ppc64le` to `_GDBSTUB_PROVEN_ARCHES`.
  `test_session_support.py` asserts that ppc64le returns unchanged and that an unrelated arch
  still skips.
- `test_debug_gdbmi_live_smoke.py`: read the program counter through an arch-keyed map
  (`x86_64: rip`, `ppc64le: pc`) from the surface profile's `arch`. Parametrize
  `test_live_vm_debug_advance_modes` over two mode groups: `single-step`
  (`into`, `over`, `instruction`) and `finish` (`out`). Each group boots its own guest. On
  ppc64le the `single-step` group calls `pytest.skip` with a reason that names #2942. The
  audit-transition assertion covers only the modes of the group that ran.
- `tests/integration/test_live_stack.py`: set `_SPINE_PC_REGISTER["ppc64le"]` and the
  unit-test parameter to `pc`. If the ppc64le spine attach passes live, provision the gdbstub
  and attach on both x86_64 and ppc64le, and delete `_SPINE_GDBSTUB_GAP` and the x86-only
  branch. If the attach fails, keep the branch and report the failure.
- `docs/development/kernel-test-cases/README.md`: update the ppc64le gdbstub status line.

### Failure model

- Actors and deployments: an operator runs `just test-live` on a native x86_64 or ppc64le
  KVM host; CI runs only the unit tests.
- Invariants: the x86_64 advance proof still runs all four modes against the same assertions.
- Accepted: one extra guest boot per x86_64 advance run (bounded cost, about one minute).
- Accepted: the `finish` group on ppc64le is unobserved before this change; the live run
  settles it, and a failure goes to #2942.
- Covered elsewhere: runtime single-step and watchpoints (#2942); TCG and remote-libvirt
  (operator).

## Success

1. On ppc64le the promoted-ops smoke and the early-boot panic attach proofs pass. The advance
   proof's `finish` group passes, and its `single-step` group skips naming #2942.
2. The spine uses `pc` on ppc64le.
3. The unit tests in the three changed test modules pass on the branch.

## Validation

- Contract: arch gate admits ppc64le. Mode: focused-test.
  `test_require_live_gdbstub_arch_returns_ppc64le_unchanged` is red against the old set;
  green with `uv run python -m pytest tests/mcp/debug/test_session_support.py -q`.
- Contract: spine register map. Mode: focused-test.
  `test_spine_gdbstub_profile_and_register[ppc64le-pc]` is red against `nip`; green with
  `uv run python -m pytest tests/integration/test_live_stack.py -k gdbstub_profile -q`.
- Contract: advance-mode groups, skip reason, and PC map. Mode: task-test-not-applicable.
  The proof is `live_vm` and needs a KVM guest, so only the live run on the native ppc64le
  host observes it. Live verification runs on a native ppc64le (POWER9) KVM-HV host.
- Contract: README status line. Mode: task-test-not-applicable. The line is prose with no
  executable consumer.
