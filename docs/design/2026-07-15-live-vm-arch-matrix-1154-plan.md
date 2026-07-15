# `live_vm_tcg` tier + guest-arch gate — Implementation Plan (#1154)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the four one-off ppc64le TCG proofs a repeatable tier via a `live_vm_tcg` marker and a discovery-driven `require_guest_arch` skip gate, with docs.

**Architecture:** `live_vm_tcg` is an *orthogonal tier tag* added on top of the four proofs' existing `live_stack` marker in `tests/integration/test_live_stack.py`; a pure `require_guest_arch(arch)` skip gate (reusing the #1153 `qemu_system_binary` map) reroutes their shared emulator check; a non-gated meta-test pins tier membership; a `just test-live-tcg` recipe selects the tier. Test-infra + docs only — no production code, no migration.

**Tech Stack:** Python 3.14, pytest (markers, `--strict-markers`), `uv`, `just`, `ruff`, `ty`.

**Spec:** `docs/design/2026-07-15-live-vm-arch-matrix-1154.md` · **ADR:** `docs/adr/0353-live-vm-tcg-tier.md`

## Global Constraints

- **Branch:** `feat/live-vm-arch-matrix-1154` off `main`. Never commit to `main`.
- **Guardrails (CI gates recipes individually):** `just lint` (ruff check + format), `just type` (ty, whole tree incl. tests), `just test` (`-m "not live_vm and not live_stack"`, `-n auto`), `just docs-links`, `just adr-status-check`. Run the relevant subset before each commit; zero warnings.
- **Line length 100; absolute imports only; ruff set `E,F,I,UP,B,SIM`.**
- **Doc-style guard:** plain factual prose; use "Milestone" not "Sprint"; avoid "critical/robust/comprehensive/elegant".
- **The gate resolves NO accelerator** (spec decision): accel is libvirt's capability advertisement, persisted by the provider; a locally-probed accel would diverge on native POWER. `require_guest_arch` returns `None`.
- **Reuse `kdive.diagnostics.guest_arch_accel.qemu_system_binary`** — no second qemu-binary literal in the test tree.
- **Conventional commits**, imperative ≤72-char subject, ending with:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- Stage **explicit paths** only — never `git add -A`.

---

### Task 1: Register the `live_vm_tcg` marker

**Files:**
- Modify: `pyproject.toml` (the `[tool.pytest.ini_options]` `markers = [...]` list, currently ~lines 109–113)

**Interfaces:**
- Produces: the `live_vm_tcg` marker, so `--strict-markers` collection does not error on it (consumed by Tasks 3, 5).

- [ ] **Step 1: Add the marker entry.** Insert after the `live_stack` line in the `markers` list:

```toml
  "live_vm_tcg: an emulated foreign-arch (TCG) guest proof — the ppc64le provision→boot→crash→retrieve spine, run over the live_stack vehicle; skips cleanly without the foreign qemu emulator (see docs/adr/0353-live-vm-tcg-tier.md)",
```

- [ ] **Step 2: Verify the marker is registered (no test yet → exit 5, not an unknown-marker error).**

Run: `uv run python -m pytest -m live_vm_tcg --collect-only --strict-markers -q; echo "exit=$?"`
Expected: `no tests ran` / `exit=5` (clean "nothing collected"), **not** `'live_vm_tcg' not found in markers configuration`.

- [ ] **Step 3: Lint + commit.**

```bash
just lint
git add pyproject.toml
git commit -m "test(1154): register the live_vm_tcg pytest marker

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Add the `require_guest_arch` skip gate + unit tests

**Files:**
- Modify: `tests/integration/live_stack/conftest.py`
- Create: `tests/integration/live_stack/test_require_guest_arch.py`

**Interfaces:**
- Consumes: `kdive.diagnostics.guest_arch_accel.qemu_system_binary(arch) -> str | None`.
- Produces: `require_guest_arch(arch: str, *, which: Callable[[str], str | None] = shutil.which) -> None` in `conftest.py` — a pure skip gate. Consumed by Task 3.

- [ ] **Step 1: Write the failing unit tests.** Create `tests/integration/live_stack/test_require_guest_arch.py`:

```python
"""Unit coverage for the require_guest_arch skip gate (#1154, ADR-0353)."""

from __future__ import annotations

import pytest

from tests.integration.live_stack.conftest import require_guest_arch


def test_returns_none_when_emulator_on_path() -> None:
    # A known arch whose emulator `which` resolves → gate passes (returns None, no skip).
    assert require_guest_arch("ppc64le", which=lambda _binary: "/usr/bin/qemu-system-ppc64") is None


def test_skips_when_emulator_absent() -> None:
    # Known arch, emulator not on PATH → clean skip.
    with pytest.raises(pytest.skip.Exception):
        require_guest_arch("ppc64le", which=lambda _binary: None)


def test_skips_when_arch_unknown_to_map() -> None:
    # An arch with no qemu_system_binary entry → clean skip (defensive floor, never a crash).
    with pytest.raises(pytest.skip.Exception):
        require_guest_arch("s390x", which=lambda _binary: "/usr/bin/whatever")
```

- [ ] **Step 2: Run to verify failure.**

Run: `uv run python -m pytest tests/integration/live_stack/test_require_guest_arch.py -q`
Expected: FAIL — `ImportError: cannot import name 'require_guest_arch'`.

- [ ] **Step 3: Implement the gate in `conftest.py`.** Add these imports at the top (beside the existing `import os`):

```python
import shutil
from collections.abc import Callable

from kdive.diagnostics.guest_arch_accel import qemu_system_binary
```

Then add the function beside `require_stack`:

```python
def require_guest_arch(
    arch: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> None:
    """Skip unless this host can boot ``arch`` guests (its system emulator is on PATH).

    A pure skip gate (ADR-0353): it reuses the #1153 ``qemu_system_binary`` map (single source)
    and resolves **no** accelerator — the provider persists that from libvirt capabilities, and
    the #1144 proof asserts the persisted value. Skips (never errors) when the arch is unknown to
    the map or its emulator is not on PATH.
    """
    binary = qemu_system_binary(arch)
    if binary is None:
        pytest.skip(f"no qemu system emulator known for guest arch {arch!r}")
    if which(binary) is None:
        pytest.skip(
            f"{binary} not on PATH; a {arch} guest boots under TCG emulation on a foreign-arch "
            f"host — install the {arch} qemu system emulator"
        )
```

- [ ] **Step 4: Run to verify pass.**

Run: `uv run python -m pytest tests/integration/live_stack/test_require_guest_arch.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Type-check + lint.**

Run: `just type && just lint`
Expected: clean (no `ty`/ruff errors on the two files).

- [ ] **Step 6: Commit.**

```bash
git add tests/integration/live_stack/conftest.py tests/integration/live_stack/test_require_guest_arch.py
git commit -m "test(1154): add require_guest_arch discovery-driven skip gate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Dual-mark the four proofs and reroute their emulator gate

**Files:**
- Modify: `tests/integration/test_live_stack.py`
  - Remove `_PPC64LE_EMULATOR = "qemu-system-ppc64"` (line ~83) and the now-unused `import shutil` (line ~34).
  - Add `import` of the gate: `from tests.integration.live_stack.conftest import require_guest_arch` (extend the existing import from that module at line ~52).
  - Reroute `_ppc64le_reachability_preflight` (the `shutil.which(_PPC64LE_EMULATOR)` block, lines ~823–827).
  - Add `@pytest.mark.live_vm_tcg` above each of the four proofs (decorators at lines ~848, ~999, ~1200, ~1382).
  - **Keep** `assert data_str(got, "accel") == "tcg"` (line ~916) unchanged.

**Interfaces:**
- Consumes: `require_guest_arch("ppc64le")` (Task 2); the `live_vm_tcg` marker (Task 1).

- [ ] **Step 1: Add the `live_vm_tcg` marker to each of the four proofs.** For each function below, insert `@pytest.mark.live_vm_tcg` immediately after its existing `@pytest.mark.live_stack` line:
  - `test_ppc64le_guest_is_ssh_reachable_over_the_wire` (~848)
  - `test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire` (~999)
  - `test_ppc64le_fadump_captures_a_vmcore_under_tcg` (~1200)
  - `test_ppc64le_kdump_captures_a_vmcore_under_tcg` (~1382)

Each becomes:

```python
@pytest.mark.live_stack
@pytest.mark.live_vm_tcg
def test_ppc64le_...(...) -> None:
```

- [ ] **Step 2: Reroute the emulator gate.** In `_ppc64le_reachability_preflight`, replace:

```python
    if shutil.which(_PPC64LE_EMULATOR) is None:
        pytest.skip(
            f"{_PPC64LE_EMULATOR} not on PATH; a ppc64le guest boots under TCG emulation on the "
            "x86_64 host — install qemu-system-ppc (the pseries emulator)"
        )
```

with:

```python
    require_guest_arch("ppc64le")
```

- [ ] **Step 3: Remove the dead constant + import.** Delete `_PPC64LE_EMULATOR = "qemu-system-ppc64"` (~line 83) and the now-unused `import shutil` (~line 34). Extend the existing conftest import (line ~52) to include `require_guest_arch`:

```python
from tests.integration.live_stack.conftest import require_guest_arch, require_issuer, require_stack
```

- [ ] **Step 4: Verify the tier collections.**

Run:
```bash
uv run python -m pytest -m live_vm_tcg --collect-only -q | tail -1
uv run python -m pytest -m "live_vm and not live_vm_tcg" --collect-only -q | tail -1
uv run python -m pytest -m "not live_vm and not live_stack" --collect-only -q | tail -1
```
Expected: `4 ... tests collected` (tcg tier); `11 ... tests collected` (native `live_vm`, unchanged); the `just test` selector count unchanged from `main` (the four stay excluded via `live_stack`).

- [ ] **Step 5: Lint + type (catches the removed-import / unused-`shutil` cleanup).**

Run: `just lint && just type`
Expected: clean — ruff reports no `F401` for `shutil` and no undefined `_PPC64LE_EMULATOR`.

- [ ] **Step 6: Commit.**

```bash
git add tests/integration/test_live_stack.py
git commit -m "test(1154): dual-mark the four ppc64le proofs live_vm_tcg + reroute gate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Pin tier membership with a non-gated meta-test

**Files:**
- Create: `tests/integration/test_live_vm_tcg_tier.py`

**Interfaces:**
- Consumes: nothing at runtime — it AST-walks the test tree. Runs in the ordinary `just test` suite (no live marker).

- [ ] **Step 1: Write the meta-test.** Create `tests/integration/test_live_vm_tcg_tier.py`:

```python
"""Non-gated guard: pin exactly the four proofs to both live_stack and live_vm_tcg (#1154).

Runs in ordinary CI (no live marker), like tests/images/test_exit_criteria.py's tier pin. Because
`just test-live-tcg` tolerates "no tests collected" as a clean skip, an emptied `-m live_vm_tcg`
selection would read green; this guard fails at the source if a marker is dropped or strays.
"""

from __future__ import annotations

import ast
import pathlib

_TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent
_EXPECTED = {
    "test_ppc64le_guest_is_ssh_reachable_over_the_wire",
    "test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire",
    "test_ppc64le_kdump_captures_a_vmcore_under_tcg",
    "test_ppc64le_fadump_captures_a_vmcore_under_tcg",
}


def _marker_names(func: ast.FunctionDef) -> set[str]:
    """The `@pytest.mark.NAME` names on a function (with or without call args)."""
    names: set[str] = set()
    for dec in func.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        ):
            names.add(node.attr)
    return names


def _functions_with_marker(marker: str) -> dict[str, set[str]]:
    """Map every test function in the tree carrying ``marker`` to its full marker set."""
    found: dict[str, set[str]] = {}
    for path in _TESTS_ROOT.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                markers = _marker_names(node)
                if marker in markers:
                    found[node.name] = markers
    return found


def test_exactly_the_four_proofs_carry_live_vm_tcg() -> None:
    carriers = _functions_with_marker("live_vm_tcg")
    assert set(carriers) == _EXPECTED, (
        "live_vm_tcg must tag exactly the four ppc64le spine proofs; "
        f"unexpected/missing: {set(carriers) ^ _EXPECTED}"
    )


def test_each_live_vm_tcg_proof_is_also_live_stack() -> None:
    carriers = _functions_with_marker("live_vm_tcg")
    for name, markers in carriers.items():
        assert "live_stack" in markers, f"{name} carries live_vm_tcg but not live_stack"
```

- [ ] **Step 2: Run to verify it passes (markers applied in Task 3).**

Run: `uv run python -m pytest tests/integration/test_live_vm_tcg_tier.py -q`
Expected: PASS (2 passed).

- [ ] **Step 3: Verify it FAILS when a marker is dropped (mutation check).** Temporarily delete one `@pytest.mark.live_vm_tcg` line from `test_live_stack.py`, rerun the meta-test, confirm FAIL, then restore.

Run: `uv run python -m pytest tests/integration/test_live_vm_tcg_tier.py::test_exactly_the_four_proofs_carry_live_vm_tcg -q`
Expected (with a marker removed): FAIL naming the missing proof. Restore the line and confirm PASS again.

- [ ] **Step 4: Lint + type + commit.**

```bash
just lint && just type
git add tests/integration/test_live_vm_tcg_tier.py
git commit -m "test(1154): pin live_vm_tcg tier membership with a non-gated guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Add `just test-live-tcg` and narrow `just test-live`

**Files:**
- Modify: `justfile` (the `test-live` recipe ~lines 76–78; add `test-live-tcg` after it, modeled on `test-live-stack` ~lines 108–119)

**Interfaces:**
- Consumes: the `live_vm_tcg` marker (Task 1) and the four marked proofs (Task 3).

- [ ] **Step 1: Narrow `test-live` to exclude the TCG tier.** Change the recipe body:

```make
# Run the live_vm suite (needs a KVM/libvirt host with a kdump-enabled guest). Native tier only;
# the emulated foreign-arch tier is `just test-live-tcg`.
test-live:
    uv run python -m pytest -m "live_vm and not live_vm_tcg" -q
```

- [ ] **Step 2: Add the `test-live-tcg` recipe** (after `test-live`), mirroring `test-live-stack`'s exit-5-clean-skip idiom:

```make
# Run the emulated foreign-arch (TCG) tier: the four ppc64le provision→boot→crash→retrieve proofs.
# Needs the foreign qemu emulator (e.g. qemu-system-ppc64) AND a running stack (`just stack-up` +
# VM fixtures); the tests skip cleanly without either. --strict-markers fails a mis-marked test;
# pytest exit 5 ("no tests collected") is tolerated as a clean skip, other codes propagate.
test-live-tcg:
    #!/usr/bin/env bash
    set -euo pipefail
    rc=0
    uv run python -m pytest -m live_vm_tcg --strict-markers -q || rc=$?
    if [[ "$rc" -eq 5 ]]; then
      echo "no live_vm_tcg tests collected — skipping cleanly (marked suite absent)"
      exit 0
    fi
    exit "$rc"
```

- [ ] **Step 3: Verify both recipes parse and behave.**

Run:
```bash
just --list | grep -E 'test-live(-tcg)?'            # both recipes present + parse
uv run python -m pytest -m "live_vm and not live_vm_tcg" --collect-only -q | tail -1
just test-live-tcg; echo "exit=$?"
```
Expected: `just --list` shows both `test-live` and `test-live-tcg`. `test-live` selector collects 11 (native, unchanged). On this x86_64 host, `just test-live-tcg` collects the 4 and each **skips** at runtime (no emulator, or stack down) — `exit=0`, printing skips (or the exit-5 clean-skip line if the marked suite were absent). No error, no failure.

- [ ] **Step 4: Commit.**

```bash
git add justfile
git commit -m "test(1154): add just test-live-tcg; narrow just test-live to native

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Document the three live tiers

**Files:**
- Modify: `AGENTS.md` (the Commands table + a short tier note near the `live_vm`/`live_stack` conventions)
- Modify: `docs/operating/runbooks/image-lifecycle.md` (the cross-arch section extended by #1153) — add the tier table + `test-live-tcg` prerequisites

**Interfaces:** none — documentation of Tasks 1–5.

- [ ] **Step 1: Add `test-live-tcg` to the AGENTS.md Commands table.** Insert a row after the `just test-live` row:

```markdown
| `just test-live-tcg` | the emulated foreign-arch (TCG) tier: the four ppc64le provision→boot→crash→retrieve proofs; needs the foreign qemu emulator + a running stack, and skips cleanly without either |
```

- [ ] **Step 2: Add a three-tier note** to AGENTS.md beside the existing `live_vm`/`live_stack` bullets (Conventions section):

```markdown
- **Three live tiers.** `live_vm` (native, direct-provider ops against a pre-provisioned System);
  `live_vm_tcg` (emulated foreign-arch spine — the ppc64le proofs, run over the `live_stack`
  vehicle, selected by `just test-live-tcg`); `live_stack` (full MCP HTTP transport). `just
  test-live` is native-only (`-m "live_vm and not live_vm_tcg"`); the TCG tier skips cleanly on a
  host without the foreign qemu emulator (`require_guest_arch`, ADR-0353).
```

- [ ] **Step 3: Add the tier table + prerequisites** to `docs/operating/runbooks/image-lifecycle.md` cross-arch section (operator voice — `python -m`/scripts, not `just`, per repo convention for operator docs; but the tier commands are the `just` recipes, so name them as the developer entrypoints and cross-reference the per-arch accel doctor check from #1153):

```markdown
### Live test tiers

| Tier | Selector | Needs |
|------|----------|-------|
| native | `just test-live` (`-m "live_vm and not live_vm_tcg"`) | a KVM/libvirt host + kdump guest image |
| emulated (TCG) | `just test-live-tcg` (`-m live_vm_tcg`) | the foreign qemu emulator (e.g. `qemu-system-ppc64`) **and** a running stack (`just stack-up` + VM fixtures) |
| wire | `just test-live-stack` (`-m live_stack`) | a running kdive stack + OIDC issuer |

The emulated tier runs the ppc64le provision→boot→force-crash→kdump-retrieve spine under TCG on an
x86_64 host and **skips cleanly** when the foreign emulator is absent (see the per-arch guest-accel
doctor check, ADR-0352, for confirming the emulator is present).
```

- [ ] **Step 4: Doc guardrails.**

Run: `just docs-links && just adr-status-check`
Expected: `markdown links resolve`; ADR index in sync.

- [ ] **Step 5: Commit.**

```bash
git add AGENTS.md docs/operating/runbooks/image-lifecycle.md
git commit -m "docs(1154): document the three live test tiers + test-live-tcg

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (before ship)

- [ ] `just lint && just type && just test` all green (the meta-test + gate unit tests run in `just test`).
- [ ] `just docs-links && just adr-status-check` green.
- [ ] Collections: `-m live_vm_tcg` = 4; `-m "live_vm and not live_vm_tcg"` = 11; `just test` selector unchanged vs `main`.
- [ ] `just test-live-tcg` on this x86_64 host skips cleanly (exit 0), and — with the stack up + the ppc64le fixtures + `qemu-system-ppc64` present — the four proofs run (live proof, recorded per the epic's proof-record convention if executed).
- [ ] No `"qemu-system-ppc64"` literal outside `guest_arch_accel.py` (AC5): `rg -n '"qemu-system-ppc64"' src tests`.

## Rollback / cleanup

Every task is additive and independently revertible. The only deletions are the dead
`_PPC64LE_EMULATOR` constant + `import shutil` in `test_live_stack.py` (Task 3); if reverted,
restore both and the `shutil.which` gate. No migration, no schema, no production code.
