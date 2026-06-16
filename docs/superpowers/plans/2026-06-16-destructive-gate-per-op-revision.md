# Destructive-gate per-op revision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Revive `systems.reprovision`, `control.power` (off/cycle/reset), and `control.force_crash` on the normal MCP path by dropping the structurally-dead `capability_scope` check from the destructive-op gate, validating profile opt-in tokens at the write boundary, and removing the dead `capability_scope` state.

**Architecture:** Three commits. (A) additive token validation at the provision/reprovision write seam; (B) the gate behavior change (three-check → two-check) plus all gate-test rewrites, field still present but unread; (C) pure dead-code removal of the `capability_scope` field/column/admission-writes plus migration `0036`. Each commit leaves the tree green and bisectable.

**Tech Stack:** Python 3.13, `uv`, `ruff`, `ty`, `pytest`; Pydantic v2 domain models; forward-only SQL migrations (`src/kdive/db/schema/NNNN_*.sql`); FastMCP tools tested directly (no transport).

**Spec:** `docs/design/destructive-gate-per-op-revision.md` · **ADR:** `docs/adr/0130-destructive-gate-per-op-revision.md`

**Guardrails (run before every commit):** `just lint && just type && just test`. Single test: `uv run python -m pytest <path>::<name> -q`. db/integration tests need a reachable Docker daemon (they skip otherwise; CI sets `KDIVE_REQUIRE_DOCKER=1`).

**Execution note:** This is a tightly-coupled change — the `capability_scope` field removal in Task C is atomic across model/repo/admission/migration/tests. Do **not** parallelize across worktrees. Execute the tasks sequentially in order A → B → C.

---

## File structure

| File | Responsibility | Task |
|------|----------------|------|
| `src/kdive/domain/models.py` | Add `DESTRUCTIVE_JOB_KINDS` runtime frozenset; later remove `Allocation.capability_scope` | A, C |
| `src/kdive/profiles/provisioning.py` | Add `ProviderSection.destructive_ops` property (active-section accessor) | A |
| `src/kdive/services/systems/validation.py` | Reject unknown `destructive_ops` tokens at the write seam | A |
| `src/kdive/security/authz/gate.py` | Drop `_scope_permits`/`_DESTRUCTIVE_OPS_KEY`/scope branch; two-check gate | B |
| `src/kdive/mcp/tools/_common.py` | Update `authz_denied` docstring enum (drop `capability_scope`) | B |
| `src/kdive/db/repositories.py` | Drop `capability_scope` from `json_columns` | C |
| `src/kdive/services/allocation/admission/core.py` | Remove the two `capability_scope={}` literals | C |
| `src/kdive/db/schema/0036_drop_allocation_capability_scope.sql` | Drop the column | C |
| gate + tool + integration tests | Rewrite three-check assertions to two-check; drop scope seeding | B, C |

---

## Task A: Validate `destructive_ops` opt-in tokens at the write boundary

**Files:**
- Modify: `src/kdive/domain/models.py` (after the `DestructiveJobKind` alias, ~line 95)
- Modify: `src/kdive/profiles/provisioning.py` (`ProviderSection`, ~line 222)
- Modify: `src/kdive/services/systems/validation.py:19-34`
- Test: add to the **existing** `tests/services/systems/test_system_validation.py` (reuse its `_VALID_PROFILE`, `_profile`, `_capabilities`, `_LOCAL_POLICY` helpers — do **not** create a new file or invent a `ComponentSourceCapabilities.none()`; that constructor does not exist — the real signature is `ComponentSourceCapabilities(provider=..., accepted_component_sources={...})`, wrapped by the file's `_capabilities(*sources)` helper).

- [ ] **Step 1: Write the failing tests**

The check runs first inside `validate_profile_for_provider`, before `profile_policy.validate_profile` and the rootfs component-source check. So the assertions must be **pinned to the token-specific signal** (`details["unknown_destructive_ops"]`), not the generic `CONFIGURATION_ERROR` category that the rootfs/policy checks also raise — otherwise a rootfs failure could make the test pass for the wrong reason. The most isolated test calls the private helper directly. Add to `tests/services/systems/test_system_validation.py`:

```python
import copy  # already imported at the top of the file

from kdive.services.systems.validation import (
    _reject_unknown_destructive_ops,  # private; imported deliberately for an isolated unit test
)


def _profile_with_ops(destructive_ops: list[str]) -> ProvisioningProfile:
    data = copy.deepcopy(_VALID_PROFILE)
    data["provider"]["local-libvirt"]["destructive_ops"] = destructive_ops
    return ProvisioningProfile.parse(data)


def test_reject_unknown_destructive_ops_flags_typo_directly() -> None:
    # Isolated: drives only the token check, no rootfs/policy/capabilities involvement.
    with pytest.raises(CategorizedError) as exc:
        _reject_unknown_destructive_ops(_profile_with_ops(["force-crash"]))  # hyphen typo
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["unknown_destructive_ops"] == ["force-crash"]


def test_reject_unknown_destructive_ops_accepts_known_directly() -> None:
    # All four closed-set tokens; the helper must not raise.
    _reject_unknown_destructive_ops(
        _profile_with_ops(["force_crash", "power", "reprovision", "teardown"])
    )


def test_validate_profile_for_provider_rejects_unknown_token() -> None:
    # Through the public seam: the token check fires first, so an otherwise-valid profile
    # (local rootfs accepted) fails ONLY on the token — pinned via details, not category.
    with pytest.raises(CategorizedError) as exc:
        validate_profile_for_provider(
            _profile_with_ops(["powercycle"]), _LOCAL_POLICY, _capabilities("local")
        )
    assert exc.value.details["unknown_destructive_ops"] == ["powercycle"]


def test_validate_profile_for_provider_accepts_known_tokens() -> None:
    # Fully valid profile (local rootfs + accepted source) with valid opt-in tokens: no raise.
    validate_profile_for_provider(
        _profile_with_ops(["force_crash", "reprovision"]), _LOCAL_POLICY, _capabilities("local")
    )
```

> Note: `_VALID_PROFILE` already uses `rootfs.kind == "local"`, which `_capabilities("local")` accepts (see the existing `test_validate_profile_for_provider_accepts_advertised_rootfs_source`), so the accept test's only variable is the token list. The `details["unknown_destructive_ops"]` key must match the key the implementation populates in Step 3c.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/services/systems/test_system_validation.py -q`
Expected: FAIL — `_reject_unknown_destructive_ops` does not exist yet (ImportError), and the public-seam reject test does not raise on the token.

- [ ] **Step 3a: Add the runtime token set to `domain/models.py`**

After the `DestructiveJobKind` `Literal` alias (~line 95) add:

```python
DESTRUCTIVE_JOB_KINDS: frozenset[JobKind] = frozenset(
    {JobKind.REPROVISION, JobKind.TEARDOWN, JobKind.FORCE_CRASH, JobKind.POWER}
)
"""Runtime set mirroring the ``DestructiveJobKind`` Literal (ADR-0130 token validation)."""
```

- [ ] **Step 3b: Add the active-section accessor to `ProviderSection`**

In `src/kdive/profiles/provisioning.py`, add a property to `ProviderSection` (alongside `kind`, ~line 192). It is a plain property, not a validator, so it never runs during `parse`/`model_validate`:

```python
    @property
    def destructive_ops(self) -> list[str]:
        """Return the active provider section's declared destructive-op opt-in tokens."""
        if self.local_libvirt_section is not None:
            return list(self.local_libvirt_section.destructive_ops)
        if self.remote_libvirt_section is not None:
            return list(self.remote_libvirt_section.destructive_ops)
        if self.fault_inject_section is not None:
            return list(self.fault_inject_section.destructive_ops)
        raise AttributeError("profile has no provider section")
```

- [ ] **Step 3c: Reject unknown tokens in `validate_profile_for_provider`**

In `src/kdive/services/systems/validation.py`, add the check at the top of `validate_profile_for_provider` (before `profile_policy.validate_profile`). Import the constant and error type:

```python
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DESTRUCTIVE_JOB_KINDS

_VALID_DESTRUCTIVE_OP_VALUES = frozenset(kind.value for kind in DESTRUCTIVE_JOB_KINDS)


def _reject_unknown_destructive_ops(profile: ProvisioningProfile) -> None:
    """Reject opt-in tokens outside the closed destructive-op set (ADR-0130).

    Once profile opt-in is the load-bearing grant, a typo would be a silent permanent
    denial indistinguishable from an intentional empty list. Runs at the write boundary
    only; ``ProvisioningProfile.parse`` stays structural so the unguarded read-path parse
    in ``control._op_opt_in`` cannot raise on a stored legacy token.
    """
    unknown = sorted(
        op for op in profile.provider.destructive_ops if op not in _VALID_DESTRUCTIVE_OP_VALUES
    )
    if unknown:
        raise CategorizedError(
            "provisioning profile declares unknown destructive_ops tokens",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "unknown_destructive_ops": unknown,
                "valid_destructive_ops": sorted(_VALID_DESTRUCTIVE_OP_VALUES),
            },
        )
```

Call it first inside `validate_profile_for_provider`:

```python
def validate_profile_for_provider(
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    capabilities: ComponentSourceCapabilities,
) -> None:
    _reject_unknown_destructive_ops(profile)
    profile_policy.validate_profile(profile)
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/services/systems/test_system_validation.py -q`
Expected: PASS (all four new tests + the pre-existing ones).

Then confirm no shipped profile regresses:
Run: `uv run python -m pytest tests/mcp/lifecycle/test_systems_tools.py tests/mcp/lifecycle/test_control_tools.py -q`
Expected: PASS (every shipped/test profile uses only valid tokens — verified in spec prep).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && just test
git add src/kdive/domain/models.py src/kdive/profiles/provisioning.py \
        src/kdive/services/systems/validation.py tests/services/systems/test_system_validation.py
git commit -m "feat(systems): reject unknown destructive_ops tokens at write seam (#465)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task B: Gate becomes a two-check policy (drop the dead `capability_scope` check)

**Files:**
- Modify: `src/kdive/security/authz/gate.py` (whole module — remove `_scope_permits`, `_DESTRUCTIVE_OPS_KEY`, the scope branch; update docstring)
- Modify: `src/kdive/mcp/tools/_common.py:49-60` (docstring enum)
- Test (rewrite): `tests/security/authz/test_gate.py`
- Test (update): `tests/mcp/test_common.py:20-21`, `tests/mcp/core/test_denial_audit_middleware.py:223`
- Test (update gate expectations): `tests/mcp/lifecycle/test_control_tools.py`, `tests/integration/test_walking_skeleton.py:120-175`

- [ ] **Step 1: Rewrite the gate unit test to the two-check model (failing)**

Replace `tests/security/authz/test_gate.py` body. The `_allocation` helper no longer takes a scope dict (the field is going away in Task C, but the gate already ignores it after this task — so build a minimal Allocation without `capability_scope`). New tests:

```python
"""Tests for the two-check destructive-op gate (ADR-0130, refines ADR-0006/0020)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from kdive.domain.models import Allocation, JobKind
from kdive.domain.state import AllocationState
from kdive.mcp.auth import RequestContext
from kdive.security.authz.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.authz.rbac import Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(role: Role) -> RequestContext:
    return RequestContext(
        principal="alice", agent_session=None, projects=("proj",), roles={"proj": role}
    )


def _allocation() -> Allocation:
    return Allocation.model_validate(
        dict(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=uuid4(),
            state=AllocationState.ACTIVE,
        )
    )


def _op(opt_in: bool = True) -> DestructiveOp:
    return DestructiveOp(kind=JobKind.FORCE_CRASH, profile_opt_in=opt_in)


def test_role_and_opt_in_present_is_allowed() -> None:
    assert assert_destructive_allowed(_ctx(Role.ADMIN), _allocation(), _op(True)) is None


def test_not_admin_denied() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.OPERATOR), _allocation(), _op(True))
    assert exc.value.missing == ["admin_role"]


def test_opt_in_false_denied() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.ADMIN), _allocation(), _op(False))
    assert exc.value.missing == ["profile_opt_in"]


def test_opt_in_defaults_false() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.ADMIN), _allocation(), DestructiveOp(kind=JobKind.FORCE_CRASH)
        )
    assert exc.value.missing == ["profile_opt_in"]


def test_both_absent_lists_role_then_opt_in() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.OPERATOR), _allocation(), _op(False))
    assert exc.value.missing == ["admin_role", "profile_opt_in"]


def test_operator_required_role_allows_operator() -> None:
    assert (
        assert_destructive_allowed(
            _ctx(Role.OPERATOR),
            _allocation(),
            DestructiveOp(kind=JobKind.REPROVISION, profile_opt_in=True),
            required_role=Role.OPERATOR,
        )
        is None
    )


def test_operator_required_role_still_denies_viewer() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(
            _ctx(Role.VIEWER),
            _allocation(),
            DestructiveOp(kind=JobKind.REPROVISION, profile_opt_in=True),
            required_role=Role.OPERATOR,
        )
    assert exc.value.missing == ["operator_role"]


def test_required_role_defaults_to_admin() -> None:
    with pytest.raises(DestructiveOpDenied) as exc:
        assert_destructive_allowed(_ctx(Role.OPERATOR), _allocation(), _op(True))
    assert exc.value.missing == ["admin_role"]
```

> Note: `Allocation.model_validate` without `capability_scope` works now (the field still exists with a default) and continues to work after Task C removes it. Do not pass `capability_scope` here.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/security/authz/test_gate.py -q`
Expected: FAIL — `assert_destructive_allowed` still appends `capability_scope` to `missing`, so `test_role_and_opt_in_present_is_allowed` raises and the `missing` lists include `capability_scope`.

- [ ] **Step 3: Make the gate two-check**

Edit `src/kdive/security/authz/gate.py`: delete `_DESTRUCTIVE_OPS_KEY` (line 30), delete `_scope_permits` (lines 53-55), delete the scope branch in `assert_destructive_allowed` (lines 80-81), and update the module + function docstrings from "three independent checks" / "all three" to "two independent checks" / "both". Resulting check body:

```python
    missing: list[str] = []
    try:
        require_role(ctx, allocation.project, required_role)
    except AuthorizationError:
        missing.append(f"{required_role.value}_role")
    if not op.profile_opt_in:
        missing.append("profile_opt_in")
    if missing:
        raise DestructiveOpDenied(missing)
```

Remove the now-unused `Allocation` import only if nothing else in the module references it (the `op`/`ctx` types remain; keep the `TYPE_CHECKING` `Allocation` import if `assert_destructive_allowed`'s signature still annotates `allocation: Allocation`). The function keeps `allocation` (used for `allocation.project`).

- [ ] **Step 4: Update the `authz_denied` docstring enum**

In `src/kdive/mcp/tools/_common.py:52-54`, drop `capability_scope` from the documented closed enum:

```python
    ``missing_checks`` is the destructive-op gate's closed enum of policy-check tokens
    (``admin_role``/``operator_role``, ``profile_opt_in``) — never a resource identifier — so
    it is safe to surface in ``data`` under the no-leak seam (ADR-0123), which suppresses
    ``detail`` only, not ``data``.
```

- [ ] **Step 5: Update the two direct enum-token tests**

`tests/mcp/test_common.py:20-21` — replace `capability_scope` with a current token:

```python
    resp = authz_denied("sys-9", ["operator_role", "profile_opt_in"])
    assert resp.data["missing_checks"] == ["operator_role", "profile_opt_in"]
```

`tests/mcp/core/test_denial_audit_middleware.py:223` — replace the sample missing check:

```python
        migrated_url, DestructiveOpDenied(["admin_role"]), expect_type=DestructiveOpDenied
```

> Verify the surrounding assertions in that test don't string-match `capability_scope`; if they do, update them to `admin_role` too.

- [ ] **Step 6: Update tool/integration gate-test expectations**

`tests/mcp/lifecycle/test_control_tools.py` — the gate tests previously required `capability_scope` (seeded via the local `_granted_allocation(scope=...)`) **and** profile opt-in **and** role. Now role + profile opt-in suffice. Concretely:
  - Positive cases (e.g. lines 233/267/293/330, force_crash 483) already seed both `scope={"destructive_ops":[...]}` and `destructive_ops=[...]` on the profile + admin role → still pass (scope ignored).
  - The `scope_ok` parametrization around line 457 (`scope = {"destructive_ops": ["force_crash"]} if scope_ok else {}`) loses its meaning: a `scope_ok=False` case that expected `capability_scope` in `missing_checks` must be rewritten to drive the **profile opt-in** axis instead (no profile opt-in → `missing_checks=["profile_opt_in"]`) and the role axis. Rewrite that parametrized test so its axes are (role_ok, opt_in_ok) and the expected `missing_checks` are drawn from `{admin_role, profile_opt_in}`. Drop the `scope` axis entirely.

`tests/integration/test_walking_skeleton.py:120-175` — the parametrized gate test has a `(scope_ok, role_ok, opt_in_ok, expected_missing)` row including `(False, True, True, "capability_scope")` (line 127) and seeds `seed_granted_allocation(capability_scope=scope)` (line 141, 172). Rewrite to a two-axis `(role_ok, opt_in_ok, expected_missing)` table; delete the `capability_scope` row; the all-pass row now needs only role + profile opt-in. Drop the `capability_scope=` seeding argument (the `seed_granted_allocation` param is removed in Task C — for this task you may leave the call passing `capability_scope={}` if needed to compile, but prefer removing the arg now and removing the param in Task C; simplest is to stop passing it here and defer the param removal to Task C).

- [ ] **Step 7: Run the affected suites**

Run:
```bash
uv run python -m pytest tests/security/authz/test_gate.py tests/mcp/test_common.py \
  tests/mcp/core/test_denial_audit_middleware.py tests/mcp/lifecycle/test_control_tools.py \
  tests/mcp/lifecycle/test_systems_tools.py tests/integration/test_walking_skeleton.py -q
```
Expected: PASS. (Integration tests skip cleanly if Docker is absent — run with Docker where possible; CI will enforce.)

- [ ] **Step 8: Guardrails + commit**

```bash
just lint && just type && just test
git add src/kdive/security/authz/gate.py src/kdive/mcp/tools/_common.py \
        tests/security/authz/test_gate.py tests/mcp/test_common.py \
        tests/mcp/core/test_denial_audit_middleware.py tests/mcp/lifecycle/test_control_tools.py \
        tests/integration/test_walking_skeleton.py
git commit -m "feat(security): drop dead capability_scope check; two-check gate (#465)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task C: Remove the dead `capability_scope` field, column, and admission writes

**Files:**
- Modify: `src/kdive/domain/models.py:260` (remove field)
- Modify: `src/kdive/db/repositories.py:261` (drop from `json_columns`)
- Modify: `src/kdive/services/allocation/admission/core.py:461,606` (remove `capability_scope={}`)
- Create: `src/kdive/db/schema/0036_drop_allocation_capability_scope.sql`
- Test (update): `tests/db/test_migrate.py` (append `"0036"`), `tests/domain/test_models.py:91`, `tests/db/test_repositories.py:191`, `tests/mcp/ops/test_ops_tuning.py:113`, `tests/mcp/catalog/test_shapes_tools.py:147`, `tests/integration/_seed.py:161-187`, `tests/integration/test_m1_allocation_accounting.py:942,991,1081`, `tests/integration/live_stack/spine.py:170-180`, plus the local `_granted_allocation` in `tests/mcp/lifecycle/test_control_tools.py:107-134`.

- [ ] **Step 1: Write the migration (the schema "test" is `test_migrate.py`)**

Create `src/kdive/db/schema/0036_drop_allocation_capability_scope.sql`:

```sql
-- 0036: Drop the structurally-dead allocations.capability_scope column (ADR-0130, #465).
-- The destructive-op gate no longer reads it; admission always wrote '{}'. The grant layer
-- is replaced by the role + profile-opt-in two-check gate, not deprecated.
ALTER TABLE allocations DROP COLUMN capability_scope;
```

- [ ] **Step 2: Update `test_migrate.py` to expect 0036 (failing first)**

In `tests/db/test_migrate.py` `test_rerun_is_a_noop`, append `"0036",` to the applied-versions list after `"0035"` (~line 131).

Run: `uv run python -m pytest tests/db/test_migrate.py -q`
Expected: FAIL until the model/repo stop referencing the dropped column (the schema apply succeeds, but `Allocation` round-trips elsewhere break). Proceed to remove the field.

- [ ] **Step 3: Remove the model field**

`src/kdive/domain/models.py:260` — delete:

```python
    capability_scope: dict[str, Any] = Field(default_factory=dict)
```

If `Any` becomes unused after this removal, drop it from the `typing` import (let `ruff`/`ty` confirm).

- [ ] **Step 4: Drop the column from the repository serializer**

`src/kdive/db/repositories.py:261` — change:

```python
    json_columns=frozenset({"capability_scope", "pcie_claim", "requested_pcie_specs"}),
```
to:
```python
    json_columns=frozenset({"pcie_claim", "requested_pcie_specs"}),
```

- [ ] **Step 5: Remove the admission writes**

`src/kdive/services/allocation/admission/core.py` — delete the `capability_scope={},` line in **both** `Allocation(...)` constructors (lines 461 and 606).

- [ ] **Step 6: Strip `capability_scope`/`scope` from the remaining test constructors and seeders**

- `tests/mcp/lifecycle/test_control_tools.py:107-134` — remove the `scope` parameter from the local `_granted_allocation` and the `capability_scope=scope or {}` line; update its call sites to drop `scope=...`.
- `tests/domain/test_models.py:91` — remove the `capability_scope={"transports": ["gdbstub"]}` kwarg from the Allocation construction (and any assertion on it).
- `tests/db/test_repositories.py:191` — drop `capability_scope={"cpus": 4}` from the `_allocation(...)` call and the `_allocation` helper's param if it has one; remove any round-trip assertion on the field.
- `tests/mcp/ops/test_ops_tuning.py:113` and `tests/mcp/catalog/test_shapes_tools.py:147` — remove the `capability_scope={},` kwarg.
- `tests/integration/_seed.py:161-187` — remove the `capability_scope` parameter from `seed_granted_allocation` and the `capability_scope=capability_scope or {}` line.
- `tests/integration/test_m1_allocation_accounting.py:942,991,1081` — remove the three `UPDATE allocations SET capability_scope = ...` raw-SQL blocks and rewrite those tests to grant via profile opt-in + role (the System's profile `destructive_ops` + the caller role), or delete the now-meaningless scope-seeding if the test's subject is unrelated to the gate. Inspect each: if the test only seeded scope to make a destructive op pass, switch it to seed the profile opt-in; if scope was incidental, just delete the UPDATE.
- `tests/integration/live_stack/spine.py:170-180` — remove the `UPDATE allocations SET capability_scope = %s::jsonb ...` block and the docstring reference to `seed_granted_allocation(capability_scope=…)`; the spine's destructive ops now rely on the profile opt-in already present in the seeded profile + the operator role. If the spine has no profile opt-in for the destructive op it exercises, add the op to the seeded profile's `destructive_ops` instead.

- [ ] **Step 7: Verify nothing references `capability_scope` anymore**

Run: `rg -n 'capability_scope' src/ tests/`
Expected: no matches (every reference removed).

- [ ] **Step 8: Run the full suite + migration test**

Run: `just test` (with Docker available so db/integration/migration tests run).
Expected: PASS, including `tests/db/test_migrate.py` (0036 applies and re-run is a no-op).

- [ ] **Step 9: Guardrails + commit**

```bash
just lint && just type && just test
git add src/kdive/domain/models.py src/kdive/db/repositories.py \
        src/kdive/services/allocation/admission/core.py \
        src/kdive/db/schema/0036_drop_allocation_capability_scope.sql \
        tests/db/test_migrate.py tests/domain/test_models.py tests/db/test_repositories.py \
        tests/mcp/ops/test_ops_tuning.py tests/mcp/catalog/test_shapes_tools.py \
        tests/mcp/lifecycle/test_control_tools.py tests/integration/_seed.py \
        tests/integration/test_m1_allocation_accounting.py tests/integration/live_stack/spine.py
git commit -m "refactor(db): drop dead allocations.capability_scope column (#465)" \
  -m "Migration 0036 drops the column the two-check gate no longer reads (ADR-0130)." \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-review checklist (run before declaring the plan done)

1. **Spec coverage:** AC1 (positive end-to-end revival) → Task B test updates + Task A leaving opt-in functional; AC2-4 (denial `missing_checks` + audit shape) → Task B gate tests; AC5 (field/column/helper removed) → Task C; AC6 (`_common` enum) → Task B Step 4; AC7 (no test constructs/seeds `capability_scope`) → Task C Step 6-7; AC8 (unknown-token rejection) → Task A; AC9 (teardown/power-on unchanged) → not modified, regression-covered by the existing suites in Task B/C. All covered.
2. **Placeholder scan:** Task A Step 1 reuses the verified helpers in `tests/services/systems/test_system_validation.py` (`_VALID_PROFILE` local rootfs, `_capabilities("local")`, `_LOCAL_POLICY`) and pins assertions to `details["unknown_destructive_ops"]`, so no constructor is guessed and the token check cannot pass for a rootfs reason. No remaining placeholders.
3. **Type consistency:** `DESTRUCTIVE_JOB_KINDS` (models) ↔ `_VALID_DESTRUCTIVE_OP_VALUES` (validation) ↔ `ProviderSection.destructive_ops` (provisioning) ↔ gate `missing` tokens (`admin_role`/`operator_role`/`profile_opt_in`) are consistent across tasks.

---

## Rollback / cleanup

- Migration `0036` is forward-only (ADR-0015); there is no down-migration. If the change must be reverted before release, restore the column with a new additive migration (`ALTER TABLE allocations ADD COLUMN capability_scope jsonb NOT NULL DEFAULT '{}'::jsonb`) and revert the code commits. Because the dropped data was always `{}`, no data restoration is needed.
- Each task is an independent green commit; reverting Task C alone (restore column + re-add field/writes) leaves the two-check gate from Task B intact, which is itself a valid state.
