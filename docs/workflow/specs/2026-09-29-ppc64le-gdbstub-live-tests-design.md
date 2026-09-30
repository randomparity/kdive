# ppc64le live gdbstub tests (#2740)

## Problem

The native gdbstub proofs skip on ppc64le. The
[#2739 proof record](../../design/2026-09-29-ppc64le-gdbstub-proof-record-2739.md) shows that
POWER gdb names the program counter `pc`, and that the KVM-HV vCPU does not single-step.

## Scope

Tests only; no `src/` change and no ownership change. Part of #2736.

- `session_support.py`: add `ppc64le` to `_GDBSTUB_PROVEN_ARCHES`; drop the ppc64le/#2736
  wording from the comment, docstring, and skip message. Replace the ppc64le-skips unit test
  with a returns-unchanged test that fails (not skips) on a gate skip.
- `test_debug_gdbmi_live_smoke.py`: module-level `_PC_REGISTER` (`x86_64: rip`,
  `ppc64le: pc`) and `_ADVANCE_MODE_GROUPS` (`single-step`: `into`, `over`, `instruction`;
  `finish`: `out`). Parametrize `test_live_vm_debug_advance_modes` over the groups; each boots
  its own guest and asserts audit transitions for its own modes. On ppc64le the `single-step`
  group skips, naming #2942, before any preflight or boot. Arch comes from the surface profile.
- `test_live_stack.py`: `_SPINE_PC_REGISTER["ppc64le"]` and its unit parameter become `pc`.
  Only when the ppc64le spine reaches and passes its attach phase live: provision the gdbstub
  on both arches, delete the x86-only attach branch and `_SPINE_GDBSTUB_GAP`, and assert
  `gdbstub_provisioned` is true for both. If the spine skips or fails first, keep the branch
  and say so in the PR.
- `docs/development/kernel-test-cases/README.md`: the ppc64le line states that breakpoints,
  `finish`, and early-boot attach are proven, and that single-step and watchpoints are not (#2942).

### Failure model

- Actors and deployments: an operator runs the `live_vm` and `live_stack` proofs on a native
  x86_64 or ppc64le KVM host; CI runs only the unit tests.
- Invariants: the x86_64 advance proof still runs all four modes with the same assertions.
- Accepted: one extra guest boot per x86_64 advance run (bounded, about one minute).
- Accepted: none for a ppc64le `finish` failure; it stops this unit for operator escalation,
  because `out` is not in the #2942 exclusion.
- Covered elsewhere: single-step and watchpoints (#2942); TCG and remote-libvirt (operator).

## Success

1. On ppc64le: smoke and early-boot attach pass; advance `finish` passes; advance
   `single-step` skips naming #2942.
2. The spine uses `pc` on ppc64le.
3. The unit tests in the three changed test modules pass.

## Validation

- Arch gate admits ppc64le. focused-test: the returns-unchanged test fails against the old
  set; `uv run python -m pytest tests/mcp/debug/test_session_support.py -q`.
- Spine register map. focused-test: `[ppc64le-pc]` fails against `nip`;
  `uv run python -m pytest tests/integration/test_live_stack.py -k gdbstub_profile -q`.
- PC map and mode groups. focused-test: x86_64 maps to `rip` with all four modes and no skip;
  ppc64le maps to `pc` and skips `single-step` naming #2942. Red before the tables exist.
- Live proofs. task-test-not-applicable: needs a KVM guest. Live verification runs on a native
  ppc64le (POWER9) KVM-HV host: `pytest -m live_vm` on the three node IDs (vmlinux as
  `KDIVE_LIVE_VM_BZIMAGE`, ppc64le rootfs), and `-m live_stack` for the spine.
- README line. task-test-not-applicable: prose with no executable consumer.
