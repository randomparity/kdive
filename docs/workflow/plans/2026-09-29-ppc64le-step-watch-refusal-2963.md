# Refuse single-step and watchpoints on ppc64le KVM (#2963) — plan

Goal: `debug.advance` single-step modes and `debug.set_watchpoint` return a typed refusal on a
ppc64le KVM System before any gdb/MI command, so the guest stays halted.

Architecture: a static `(arch, accel)` table in the gdbmi policy package names missing
capabilities; the Debug-plane op runner reads the session System's arch and accelerator and
refuses a declared requirement after the live-session gate and before the engine. Spec:
`docs/workflow/specs/2026-09-29-ppc64le-step-watch-refusal-2963-design.md`; ADR-0712.

Tech stack: Python 3.14, fastmcp, psycopg, pytest.

Expected implementation size: 190–260 changed lines (M) — one new 35-line module, about 60 lines
in `runtime.py`/`execution.py`/`watchpoints.py`, docstring and guide lines, and about 120 lines
of unit and live test changes in Tasks 1–3.

## Global Constraints

- No new dependency, error category, or MCP parameter. x86_64 behavior does not change.
- Codes: `single_step_unsupported` (new), `watchpoint_unsupported` (existing, ADR-0277); category
  `debug_attach_failure`.
- Guardrails: `just lint`, `just type`, `just test-changed`, `just docs` then `just docs-check`,
  `just mcp-spec-check`, `just cli-verbs-check`, `just records`; pre-push `just ci`.
- Wording rules from AGENTS.md apply to docstrings and docs.

## File map

| File | Change | Owns after |
|---|---|---|
| `src/kdive/providers/shared/debug_common/gdbmi/policy/capabilities.py` | create | `DebugCapability`, `supports` |
| `src/kdive/mcp/tools/debug/operations/runtime.py` | modify | `CapabilityRequirement`, the pre-engine gate in `_run_engine_op` |
| `src/kdive/mcp/tools/debug/operations/execution.py` | modify | per-mode single-step requirement for `debug.advance` |
| `src/kdive/mcp/tools/debug/operations/watchpoints.py` | modify | watchpoint requirement for `debug.set_watchpoint` |
| `docs/guide/reference/debug.md` | regenerate | tool docstrings |
| `docs/guide/toolsets/debug.md` | modify | one sentence on the refusal |
| `tests/providers/debug_common/test_gdbmi_capabilities.py` | create | table tests |
| `tests/mcp/debug/session_support.py` | modify | `accel` on `seed_system` / `seed_live_session` |
| `tests/mcp/debug/test_debug_ops.py` | modify | gate tests through the registered tools' runner |
| `tests/mcp/debug/test_debug_gdbmi_live_smoke.py` | modify | refusal assertions replace the #2740 skip |

No caller migration: the new `requires` keyword defaults to `None`, so every other op is unchanged.

## Task 1 — capability table

Verification:
- Contract: `supports` refuses only named `(arch, accel, capability)` triples.
  Mode: focused-test. `tests/providers/debug_common/test_gdbmi_capabilities.py`; red:
  `ModuleNotFoundError` before the module exists; green:
  `uv run python -m pytest tests/providers/debug_common/test_gdbmi_capabilities.py -q`.

Steps:
1. Write the test:

```python
import pytest

from kdive.providers.shared.debug_common.gdbmi.policy.capabilities import (
    DebugCapability,
    supports,
)


@pytest.mark.parametrize("capability", list(DebugCapability))
def test_ppc64le_kvm_lacks_single_step_and_hw_watchpoints(capability: DebugCapability) -> None:
    assert supports("ppc64le", "kvm", capability) is False


@pytest.mark.parametrize(
    ("arch", "accel"),
    [("x86_64", "kvm"), ("x86_64", "tcg"), ("ppc64le", "tcg"), ("ppc64le", None), (None, "kvm")],
)
@pytest.mark.parametrize("capability", list(DebugCapability))
def test_other_or_unknown_targets_keep_every_capability(
    arch: str | None, accel: str | None, capability: DebugCapability
) -> None:
    assert supports(arch, accel, capability) is True
```

2. Run it; expect `ModuleNotFoundError`.
3. Create the module:

```python
"""Debug capabilities a gdbstub target lacks, per architecture and accelerator (ADR-0712)."""

from __future__ import annotations

from enum import StrEnum


class DebugCapability(StrEnum):
    SINGLE_STEP = "single_step"
    HW_WATCHPOINT = "hw_watchpoint"


# Native POWER9 proof (#2739): the pseries gdbstub under KVM neither stops after a single-step
# nor inserts a hardware watchpoint. A new row needs the same kind of native evidence.
_UNSUPPORTED: dict[tuple[str, str], frozenset[DebugCapability]] = {
    ("ppc64le", "kvm"): frozenset({DebugCapability.SINGLE_STEP, DebugCapability.HW_WATCHPOINT}),
}


def supports(arch: str | None, accel: str | None, capability: DebugCapability) -> bool:
    """Return False only for a known ``(arch, accel)`` whose row names ``capability``."""
    if arch is None or accel is None:
        return True
    return capability not in _UNSUPPORTED.get((arch, accel), frozenset())
```

4. Run the green command; expect all pass. Commit.

## Task 2 — gate the two tools

Interfaces: consumes `DebugCapability` and `supports` from Task 1. Produces
`runtime.CapabilityRequirement(capability, code, detail, next_actions, data)` and the keyword
`requires: CapabilityRequirement | None = None` on `run_engine_op_with_resolver`,
`run_engine_op_with_runtime` and `_run_engine_op`.

Verification:
- Contract: on a ppc64le/kvm System, `debug.advance` `into`/`over`/`instruction` returns
  `debug_attach_failure` / `single_step_unsupported` with `mode`, `arch`, `accel`, the three next
  actions, and no written gdb/MI command. Mode: focused-test in `test_debug_ops.py`
  (`test_advance_single_step_is_refused_on_ppc64le_kvm`); red: the fake controller records the
  step verb and the status is `stopped`; green:
  `uv run python -m pytest tests/mcp/debug/test_debug_ops.py -q -k "refused or ppc64le"`.
- Contract: `out` on the same System and every mode on x86_64/kvm still dispatch their verb.
  Mode: focused-test (`test_advance_out_runs_on_ppc64le_kvm`, the existing
  `test_advance_mode_dispatches_its_gdb_verb`); green: same command plus `-k dispatches`.
- Contract: `debug.set_watchpoint` on ppc64le/kvm returns `watchpoint_unsupported` and writes no
  `-break-watch`. Mode: focused-test (`test_set_watchpoint_is_refused_on_ppc64le_kvm`); red:
  status `watching`; green: the first command.
- Contract: a refused op writes no audit row. Mode: focused-test (asserted inside the advance
  refusal test through `_audit_rows(pool, session_id) == []`).

Steps:
1. `session_support.py`: add `accel: str | None = None` to `seed_system` (set `accel=accel` on
   the `System`) and `profile: dict[str, Any] | None = None, accel: str | None = None` to
   `seed_live_session`, passed through to `seed_system`.
2. In `test_debug_ops.py` add the tests. They call the tool path's runner exactly as the
   registration does, via a helper
   `ops_execution._advance_requirement(mode)` and `ops_watchpoints._WATCHPOINT_REQUIREMENT`,
   passing `requires=` to `run_engine_op_with_runtime`. The session is seeded with
   `profile=live_profile("ppc64le")` (from `session_support`) and `accel="kvm"`. Assert
   `controller.written == []`, `resp.error_category == "debug_attach_failure"`,
   `resp.data["code"]`, `resp.data["arch"] == "ppc64le"`, `resp.data["accel"] == "kvm"`, and
   `resp.suggested_next_actions`.
3. Run; expect failures (`AttributeError: _advance_requirement`).
4. `runtime.py`: add

```python
@dataclass(frozen=True)
class CapabilityRequirement:
    """A debug capability an op needs, and the refusal to return without it (ADR-0712)."""

    capability: DebugCapability
    code: str
    detail: str
    next_actions: list[str]
    data: Mapping[str, JsonValue] = field(default_factory=dict)


async def _target_platform(pool, session) -> tuple[str | None, str | None]:
    async with pool.connection() as conn:
        run = await RUNS.get(conn, session.run_id)
        system = None if run is None else await SYSTEMS.get(conn, run.system_id)
    if system is None:
        return None, None
    arch = system.provisioning_profile.get("arch")
    return (arch if isinstance(arch, str) else None), system.accel


async def _capability_refusal(pool, session, requires) -> ToolResponse | None:
    arch, accel = await _target_platform(pool, session)
    if supports(arch, accel, requires.capability):
        return None
    return ToolResponse.failure(
        str(session.id), ErrorCategory.DEBUG_ATTACH_FAILURE, detail=requires.detail,
        suggested_next_actions=list(requires.next_actions),
        data={"code": requires.code, "arch": arch, "accel": accel, **requires.data},
    )
```

   In `_run_engine_op`, after the `_live_session` check: `if requires is not None:` call
   `_capability_refusal` and return it when not `None`. Thread `requires` through both public
   runners.
5. `execution.py`: add `_SINGLE_STEP_MODES = frozenset({"into", "over", "instruction"})` and
   `_advance_requirement(mode) -> CapabilityRequirement | None` returning `None` for other modes,
   else code `single_step_unsupported`, detail
   `"this target cannot single-step the vCPU; use debug.advance mode='out', or set a breakpoint
   and debug.continue"`, next actions `["debug.advance", "debug.set_breakpoint",
   "debug.continue"]`, `data={"mode": mode}`. Pass `requires=_advance_requirement(mode)`; add
   one docstring sentence naming `single_step_unsupported`.
6. `watchpoints.py`: add `_WATCHPOINT_REQUIREMENT` with `HW_WATCHPOINT`, code
   `watchpoint_unsupported`, detail `"this target cannot insert a hardware watchpoint; set a
   breakpoint and debug.continue"`, next actions `["debug.set_breakpoint", "debug.continue"]`;
   pass it from `debug_set_watchpoint`; add one docstring sentence.
7. Run the green commands, `just lint`, `just type`. Run `just docs`, then `just docs-check`,
   `just mcp-spec-check`, `just cli-verbs-check`; expect exit 0. Add one sentence to
   `docs/guide/toolsets/debug.md` under `debug.advance`. Commit.

## Task 3 — live proofs assert the refusal

Verification:
- Contract: the offline selector maps ppc64le single-step to `single_step_unsupported` and
  x86_64 to no refusal. Mode: focused-test — rewrite
  `test_advance_proof_reads_the_arch_pc_and_skips_only_unsupported_groups` as
  `test_advance_proof_expects_refusal_only_for_ppc64le_single_step`; green:
  `uv run python -m pytest tests/mcp/debug/test_debug_gdbmi_live_smoke.py -q -m "not live_vm"`.
- Contract: native ppc64le KVM-HV run. Mode: focused-test on the host —
  `just test-live` subset `-k "debug_advance_modes or promoted_ops_smoke"`; expect 3 passed.

Steps:
1. Replace `_UNSUPPORTED_ADVANCE_GROUPS` with `_REFUSED_ADVANCE_GROUPS = {("ppc64le",
   "single-step"): "single_step_unsupported"}` and add `_REFUSED_WATCHPOINT_ARCHES =
   frozenset({"ppc64le"})`. Drop the skip in `test_live_vm_debug_advance_modes`; pass
   `refusal=_REFUSED_ADVANCE_GROUPS.get((arch, group))` to `_drive_advance_modes`.
2. `_LiveDebugSurface.session` seeds `accel="kvm"` through `_start_live_session`.
3. `_exercise_advance_mode(..., refusal)`: when `refusal` is set, assert `advanced.status ==
   "error"`, `advanced.data["code"] == refusal`, and `after == before`; the audit-transition
   check expects only unrefused modes.
4. `_drive_gdbmi_smoke`: on a refused arch assert `watch.data["code"] ==
   "watchpoint_unsupported"`, `list_watchpoints` count `0`, and `debug.read_registers` status
   `read`; otherwise keep the set/list/clear path.
5. Run the offline command; commit. The native run is the live proof step of the quest.
