# live_vm Harness + Environment Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the reusable `live_vm` throwaway-domain harness (sub-issue A of epic #1289): a `boot_throwaway_domain` context manager, an arch-parameterized domain-XML builder, centralized env-contract resolvers + `require_live_vm_*` skip gates, additive family sub-markers, and migrate the two throwaway-domain tests onto it.

**Architecture:** The pytest-free *mechanism* ships in `src/kdive/testing/live_vm.py` (mirrors `src/kdive/mcp/dev_harness.py`, which ships live-stack client mechanism with no pytest import). The `pytest.skip`/`pytest.fail` *gates* live in `tests/live_vm/__init__.py` beside the pattern of `require_issuer`/`require_stack`. The arch branch reuses `kdive.domain.platform.arch_traits` — no re-derived `if arch ==`.

**Tech Stack:** Python 3.14, `uv`, pytest 9 (dev-only), `xml.etree.ElementTree`, `libvirt-python` (lazy-imported, live-only), `qemu-img` (subprocess).

## Global Constraints

- **Branch:** `feat/live-vm-harness-1290`; base `main`. Never commit to `main`.
- **Guardrails before every commit:** `just ci` (lint = `ruff check` + `ruff format --check`; type = `ty check` whole-tree src+tests; lint-shell; lint-workflows; check-mermaid; test). Live proof: `just test-live` (this host runs KVM/libvirt directly).
- **Ruff:** line length 100; lint set `E,F,I,UP,B,SIM`. Absolute imports only (no `..`).
- **`src/` stays pytest-free:** no `import pytest` in `src/kdive/testing/live_vm.py`. Lazy `import libvirt` / `import libvirt_qemu` inside functions only; live-only real code carries `# pragma: no cover - live_vm`.
- **Markers register under `--strict-markers`:** every new marker must be added to `pyproject.toml` `[tool.pytest.ini_options].markers` or its use errors.
- **Doc-style:** plain prose; never "critical/robust/comprehensive/elegant"; "Milestone" not "Sprint" (applies to docstrings + commit messages).
- **Commits:** Conventional Commits; imperative ≤72-char subject; stage explicit paths (never `git add -A`); end body with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Spec:** `docs/superpowers/specs/2026-07-18-live-vm-harness-1290-design.md` is authoritative. ADR: `docs/adr/0386-live-test-framework-runner-topology.md` (implemented, not modified).

## File Structure

- Create `src/kdive/testing/__init__.py` — new test-support package (empty or a one-line docstring).
- Create `src/kdive/testing/live_vm.py` — the pytest-free mechanism (env resolvers, `throwaway_domain_xml`, wait predicates, `create_overlay`, `connect_libvirt`, session-XDG helper, `LiveDomain`, `LiveVmBootTimeout`, `boot_throwaway_domain`).
- Create `tests/testing/__init__.py` and `tests/testing/test_live_vm.py` — unit tests for the mechanism (mirrors the package tree).
- Create `tests/live_vm/__init__.py` — the `require_live_vm_*` gates (imports pytest, allowed in tests).
- Create `tests/live_vm/test_gates.py` — unit tests for the gates.
- Create `tests/live_vm/test_family_markers.py` — the additivity meta-test.
- Modify `pyproject.toml` — register `live_vm_throwaway` / `live_vm_provisioned` markers.
- Modify `tests/providers/local_libvirt/test_snapshot_live.py` — migrate onto the harness.
- Modify `tests/providers/local_libvirt/test_traffic_capture_live.py` — migrate onto the harness.

---

### Task 1: Register family sub-markers + additivity meta-test

**Files:**
- Modify: `pyproject.toml` (the `[tool.pytest.ini_options].markers` list, after the `live_vm_tcg` entry)
- Create: `tests/live_vm/__init__.py` (empty for now — filled in Task 3)
- Create: `tests/live_vm/test_family_markers.py`

**Interfaces:**
- Produces: the registered markers `live_vm_throwaway`, `live_vm_provisioned`; the additivity guard that later tasks' tagging must satisfy.

- [ ] **Step 1: Add the marker registrations**

In `pyproject.toml`, the markers list currently ends with the `live_vm_tcg` entry (around line 113). Add two entries after it:

```toml
  "live_vm_throwaway: an additive live_vm sub-marker for throwaway-domain tests served by boot_throwaway_domain (kdive.testing.live_vm); the test also carries the bare live_vm marker",
  "live_vm_provisioned: an additive live_vm sub-marker for tests against an externally provisioned System (KDIVE_LIVE_VM_SYSTEM_ID + KDIVE_S3_*); the test also carries the bare live_vm marker",
```

- [ ] **Step 2: Write the additivity meta-test**

Create `tests/live_vm/test_family_markers.py`. It reuses the proven AST marker-walk idiom from `tests/integration/test_live_vm_tcg_tier.py` (per-function decorators ∪ module-level `pytestmark`), self-contained to avoid a cross-test private import:

```python
"""Non-gated guard: the live_vm family sub-markers are additive (#1290, epic #1289).

Every test carrying live_vm_throwaway or live_vm_provisioned must ALSO carry the bare live_vm
marker, so `-m live_vm` still selects both families and the shipped test-live recipe
(-m "live_vm and not live_vm_tcg") is unaffected. This asserts additivity only — NOT completeness
(every live_vm test has a family sub-marker): the debug/panic tests are un-migrated until sub-issue
E, and some live_vm tests (e.g. the retained-vmcore introspect test) fit neither family, so a
completeness guard would red-fail now. Runs in ordinary CI, like test_live_vm_tcg_tier.py.
"""

from __future__ import annotations

import ast
import pathlib
from functools import cache

_TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent
_FAMILY_SUBMARKERS = ("live_vm_throwaway", "live_vm_provisioned")


def _mark_name(node: ast.expr) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "mark"
    ):
        return target.attr
    return None


def _marks_in(node: ast.expr) -> set[str]:
    exprs = node.elts if isinstance(node, ast.List | ast.Tuple) else [node]
    return {name for expr in exprs if (name := _mark_name(expr)) is not None}


def _module_markers(tree: ast.Module) -> set[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is not None and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets
        ):
            return _marks_in(node.value)
    return set()


@cache
def _functions_with_any(markers: tuple[str, ...]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in _TESTS_ROOT.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_marks = _module_markers(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                effective = module_marks | {
                    name for dec in node.decorator_list for name in _marks_in(dec)
                }
                if effective & set(markers):
                    found[node.name] = effective
    return found


def test_every_family_submarker_test_also_carries_live_vm() -> None:
    carriers = _functions_with_any(_FAMILY_SUBMARKERS)
    offenders = {name for name, marks in carriers.items() if "live_vm" not in marks}
    assert not offenders, (
        "live_vm_throwaway/live_vm_provisioned are ADDITIVE — every carrier must also carry the "
        f"bare live_vm marker; missing on: {sorted(offenders)}"
    )
```

- [ ] **Step 3: Run the meta-test (passes vacuously — no carriers yet)**

Run: `uv run python -m pytest tests/live_vm/test_family_markers.py -q`
Expected: PASS (no test carries a family sub-marker yet, so the offender set is empty).

- [ ] **Step 4: Verify markers are registered (no strict-markers error)**

Run: `uv run python -m pytest --collect-only -q -m live_vm_throwaway 2>&1 | tail -3`
Expected: collects 0 items with **no** "Unknown pytest.mark.live_vm_throwaway" error (registration succeeded).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/live_vm/__init__.py tests/live_vm/test_family_markers.py
git commit -m "test(live-vm): register additive family sub-markers + additivity guard (#1290)"
```

---

### Task 2: Env-contract module — constants, states, resolvers

**Files:**
- Create: `src/kdive/testing/__init__.py`
- Create: `src/kdive/testing/live_vm.py` (env portion + the module docstring that documents the environment contract)
- Create: `tests/testing/__init__.py`
- Create: `tests/testing/test_live_vm.py`

**Interfaces:**
- Produces:
  - `LIVE_VM_ROOTFS_ENV = "KDIVE_LIVE_VM_ROOTFS"`, `LIVE_VM_SYSTEM_ID_ENV = "KDIVE_LIVE_VM_SYSTEM_ID"`, `LIBVIRT_URI_ENV = "KDIVE_LIBVIRT_URI"`
  - `class LiveVmEnvState(Enum)` → `AVAILABLE`, `ABSENT`, `MISCONFIGURED`
  - `ThrowawayContract(rootfs: Path, libvirt_uri: str)`, `ProvisionedContract(system_id: str, libvirt_uri: str)` (frozen slotted dataclasses)
  - `EnvResolution[T]` with `.state: LiveVmEnvState`, `.contract: T | None`, `.reason: str`
  - `resolve_throwaway_contract(default_uri: str) -> EnvResolution[ThrowawayContract]`
  - `resolve_provisioned_contract(default_uri: str) -> EnvResolution[ProvisionedContract]`

- [ ] **Step 1: Write the failing env-resolution tests**

Create `tests/testing/__init__.py` (empty) and `tests/testing/test_live_vm.py`:

```python
"""Unit tests for the pytest-free live_vm harness mechanism (kdive.testing.live_vm)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.testing.live_vm import (
    LiveVmEnvState,
    resolve_provisioned_contract,
    resolve_throwaway_contract,
)


def test_throwaway_absent_when_rootfs_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_ROOTFS", raising=False)
    result = resolve_throwaway_contract("qemu:///system")
    assert result.state is LiveVmEnvState.ABSENT
    assert "KDIVE_LIVE_VM_ROOTFS" in result.reason


def test_throwaway_misconfigured_when_rootfs_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", "/nonexistent/rootfs.qcow2")
    result = resolve_throwaway_contract("qemu:///system")
    assert result.state is LiveVmEnvState.MISCONFIGURED


def test_throwaway_misconfigured_when_parent_dir_not_writable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    rootfs = ro_dir / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    ro_dir.chmod(0o500)  # readable+executable, not writable
    try:
        result = resolve_throwaway_contract("qemu:///system")
        # env not set here; set it explicitly:
        monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
        result = resolve_throwaway_contract("qemu:///system")
        assert result.state is LiveVmEnvState.MISCONFIGURED
        assert "writable" in result.reason
    finally:
        ro_dir.chmod(0o700)


def test_throwaway_available_resolves_default_uri(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    result = resolve_throwaway_contract("qemu:///system")
    assert result.state is LiveVmEnvState.AVAILABLE
    assert result.contract is not None
    assert result.contract.libvirt_uri == "qemu:///system"
    assert result.contract.rootfs == rootfs


def test_throwaway_available_honors_libvirt_uri_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///session")
    result = resolve_throwaway_contract("qemu:///system")
    assert result.contract is not None
    assert result.contract.libvirt_uri == "qemu:///session"


def test_provisioned_absent_when_system_id_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_SYSTEM_ID", raising=False)
    result = resolve_provisioned_contract("qemu:///system")
    assert result.state is LiveVmEnvState.ABSENT


def test_provisioned_misconfigured_on_partial_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_SYSTEM_ID", "sys-123")
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.delenv("KDIVE_S3_BUCKET", raising=False)
    monkeypatch.delenv("KDIVE_S3_ACCESS_KEY_ID", raising=False)
    result = resolve_provisioned_contract("qemu:///system")
    assert result.state is LiveVmEnvState.MISCONFIGURED
    assert "KDIVE_S3_" in result.reason
```

> **Note on the S3 env names:** before implementing, confirm the exact `KDIVE_S3_*` variable names in `src/kdive/config/` (grep `KDIVE_S3_`). Use the real names in both the resolver and the test. The plan uses `KDIVE_S3_ENDPOINT_URL` / `KDIVE_S3_BUCKET` / `KDIVE_S3_ACCESS_KEY_ID` as the illustrative set — replace with the actual required set.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/testing/test_live_vm.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.testing'`.

- [ ] **Step 3: Create the package + module with the contract docstring and resolvers**

Create `src/kdive/testing/__init__.py`:

```python
"""Test-support code shipped in the package (pytest-free), imported by the live-VM test tiers."""
```

Create `src/kdive/testing/live_vm.py`. Start with the module docstring (this **is** the documented environment contract — an acceptance criterion) and the env-resolution layer:

```python
"""Reusable ``live_vm`` throwaway-domain harness + environment contract (epic #1289, sub-issue A).

This module is the single reusable way to boot a throwaway libvirt domain, wait for a chosen
condition, and tear it down, with the environment quirks encoded once. It is **pytest-free** (the
mechanism ships in ``src/`` like ``kdive.mcp.dev_harness``; the ``pytest.skip`` gates live in
``tests/live_vm``), and imports ``libvirt`` lazily so it loads on a host without it.

Environment contract (what a runner must provide; read here, not per test module):

- ``KDIVE_LIVE_VM_ROOTFS`` — a bootable qcow2 the throwaway family overlays and boots.
- ``KDIVE_LIVE_VM_SYSTEM_ID`` + the ``KDIVE_S3_*`` backend — the provisioned-System family.
- ``KDIVE_LIBVIRT_URI`` — the operator escape hatch; ``resolve_*_contract`` returns it when set,
  else the caller's ``default_uri``. ``contract.libvirt_uri`` is the single source of truth for the
  URI; a test threads it into ``boot_throwaway_domain(mode=...)``.
- libvirt mode is **per test**, not a global pin: traffic-capture uses ``qemu:///session``
  (unprivileged, dodges the ADR-0223 root-readback wall, #1258); snapshot uses ``qemu:///system``.
- Session mode: ``connect_libvirt`` redirects ``XDG_CONFIG_HOME`` to a short ``/tmp`` path for the
  QMP UNIX-socket 108-byte limit and restores it in teardown. This mutation is process-global, so
  **one session-mode boot at a time per process** (pytest-xdist workers are separate processes with
  independent ``os.environ``, so xdist is unaffected; nested/threaded same-process session boots are
  not supported).
- Staged overlays are created **beside the rootfs** so they inherit its libvirt access + SELinux
  ``virt_image_t`` label (a rootfs under ``$HOME``/``data_home_t`` is blocked at domain start under
  system mode — name it, do not silently fail).

Skip-vs-fail discipline (a skip must be distinguishable from a pass): required env unset → the gate
skips; env **set but wrong** (missing rootfs file, non-writable parent dir, partial ``KDIVE_S3_*``)
→ the gate fails loud, because a mis-provisioned runner must not masquerade as "no environment".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

LIVE_VM_ROOTFS_ENV = "KDIVE_LIVE_VM_ROOTFS"
LIVE_VM_SYSTEM_ID_ENV = "KDIVE_LIVE_VM_SYSTEM_ID"
LIBVIRT_URI_ENV = "KDIVE_LIBVIRT_URI"

# The KDIVE_S3_* variables a provisioned-System live run needs. Confirm against src/kdive/config/
# before implementing and use the real names here.
_S3_REQUIRED_ENV = ("KDIVE_S3_ENDPOINT_URL", "KDIVE_S3_BUCKET", "KDIVE_S3_ACCESS_KEY_ID")


class LiveVmEnvState(Enum):
    """Whether a live_vm family's required environment is present, absent, or set-but-wrong."""

    AVAILABLE = "available"
    ABSENT = "absent"
    MISCONFIGURED = "misconfigured"


@dataclass(frozen=True, slots=True)
class ThrowawayContract:
    rootfs: Path
    libvirt_uri: str


@dataclass(frozen=True, slots=True)
class ProvisionedContract:
    system_id: str
    libvirt_uri: str


@dataclass(frozen=True, slots=True)
class EnvResolution[T]:
    """A resolved env contract: ``state`` plus either ``contract`` (AVAILABLE) or a ``reason``."""

    state: LiveVmEnvState
    contract: T | None = None
    reason: str = ""


def _resolved_uri(default_uri: str) -> str:
    return os.environ.get(LIBVIRT_URI_ENV) or default_uri


def resolve_throwaway_contract(default_uri: str) -> EnvResolution[ThrowawayContract]:
    """Resolve the throwaway-domain family's env: rootfs + libvirt URI (see module docstring)."""
    raw = os.environ.get(LIVE_VM_ROOTFS_ENV)
    if not raw:
        return EnvResolution(
            LiveVmEnvState.ABSENT,
            reason=f"{LIVE_VM_ROOTFS_ENV} unset; point it at a bootable rootfs qcow2",
        )
    rootfs = Path(raw)
    if not rootfs.is_file():
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=f"{LIVE_VM_ROOTFS_ENV}={raw} does not point at a readable file",
        )
    if not os.access(rootfs.parent, os.W_OK):
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=(
                f"{LIVE_VM_ROOTFS_ENV}'s parent dir {rootfs.parent} is not writable — the boot "
                "stages a qcow2 overlay beside the rootfs (which must also be virt_image_t-labeled "
                "under system mode); use a writable, correctly-labeled staging dir"
            ),
        )
    return EnvResolution(
        LiveVmEnvState.AVAILABLE, ThrowawayContract(rootfs=rootfs, libvirt_uri=_resolved_uri(default_uri))
    )


def resolve_provisioned_contract(default_uri: str) -> EnvResolution[ProvisionedContract]:
    """Resolve the provisioned-System family's env: System id + S3 backend (see module docstring)."""
    system_id = os.environ.get(LIVE_VM_SYSTEM_ID_ENV)
    if not system_id:
        return EnvResolution(
            LiveVmEnvState.ABSENT,
            reason=f"{LIVE_VM_SYSTEM_ID_ENV} unset; provision a System and export its id",
        )
    missing = [name for name in _S3_REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=(
                f"{LIVE_VM_SYSTEM_ID_ENV} is set but the required object store is not fully "
                f"configured (missing: {', '.join(missing)})"
            ),
        )
    return EnvResolution(
        LiveVmEnvState.AVAILABLE,
        ProvisionedContract(system_id=system_id, libvirt_uri=_resolved_uri(default_uri)),
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/testing/test_live_vm.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check src/kdive/testing tests/testing && uv run ruff format --check src/kdive/testing tests/testing
uv run ty check
git add src/kdive/testing/__init__.py src/kdive/testing/live_vm.py tests/testing/__init__.py tests/testing/test_live_vm.py
git commit -m "feat(live-vm): env-contract resolvers for the live_vm harness (#1290)"
```

---

### Task 3: `require_live_vm_*` skip gates

**Files:**
- Modify: `tests/live_vm/__init__.py` (add the gates)
- Create: `tests/live_vm/test_gates.py`

**Interfaces:**
- Consumes: `resolve_throwaway_contract`, `resolve_provisioned_contract`, `LiveVmEnvState`, `ThrowawayContract`, `ProvisionedContract` from `kdive.testing.live_vm`.
- Produces:
  - `require_live_vm_throwaway(default_uri: str = "qemu:///system", *, session_required: bool = False) -> ThrowawayContract`
  - `require_live_vm_provisioned(default_uri: str = "qemu:///system") -> ProvisionedContract`

- [ ] **Step 1: Write the failing gate tests**

Create `tests/live_vm/test_gates.py`:

```python
"""Unit tests for the live_vm skip/fail gates (tests.live_vm)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live_vm import require_live_vm_provisioned, require_live_vm_throwaway


def test_throwaway_skips_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_ROOTFS", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_live_vm_throwaway()


def test_throwaway_fails_loud_when_misconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", "/nonexistent/rootfs.qcow2")
    with pytest.raises(pytest.fail.Exception):
        require_live_vm_throwaway()


def test_throwaway_returns_contract_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    contract = require_live_vm_throwaway("qemu:///system")
    assert contract.libvirt_uri == "qemu:///system"


def test_throwaway_session_required_fails_when_override_moves_off_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    with pytest.raises(pytest.fail.Exception):
        require_live_vm_throwaway("qemu:///session", session_required=True)


def test_throwaway_session_required_passes_on_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    monkeypatch.setenv("KDIVE_LIVE_VM_ROOTFS", str(rootfs))
    monkeypatch.delenv("KDIVE_LIBVIRT_URI", raising=False)
    contract = require_live_vm_throwaway("qemu:///session", session_required=True)
    assert contract.libvirt_uri.startswith("qemu:///session")


def test_provisioned_skips_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIVE_VM_SYSTEM_ID", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_live_vm_provisioned()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/live_vm/test_gates.py -q`
Expected: FAIL with `ImportError: cannot import name 'require_live_vm_throwaway'`.

- [ ] **Step 3: Implement the gates**

Replace `tests/live_vm/__init__.py` (was empty from Task 1) with:

```python
"""The live_vm skip/fail gates — the live_vm analogue of require_issuer / require_stack.

Thin pytest wrappers over the pytest-free resolvers in kdive.testing.live_vm: env unset skips,
env set-but-wrong fails loud. Kept in tests/ (not src/) so the shipped mechanism stays pytest-free.
"""

from __future__ import annotations

import pytest

from kdive.testing.live_vm import (
    LiveVmEnvState,
    ProvisionedContract,
    ThrowawayContract,
    resolve_provisioned_contract,
    resolve_throwaway_contract,
)


def require_live_vm_throwaway(
    default_uri: str = "qemu:///system", *, session_required: bool = False
) -> ThrowawayContract:
    """Skip if the throwaway env is absent, fail loud if misconfigured, else return the contract.

    When ``session_required`` is set and the resolved URI is not a ``qemu:///session`` URI, fail
    loud rather than boot a session-only test (#1258 root-readback) into the wrong mode.
    """
    resolution = resolve_throwaway_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    assert resolution.contract is not None
    contract = resolution.contract
    if session_required and not contract.libvirt_uri.startswith("qemu:///session"):
        pytest.fail(
            "this test requires a qemu:///session URI (#1258 root-readback); "
            f"{contract.libvirt_uri!r} was resolved from KDIVE_LIBVIRT_URI"
        )
    return contract


def require_live_vm_provisioned(default_uri: str = "qemu:///system") -> ProvisionedContract:
    """Skip if the provisioned-System env is absent, fail loud if misconfigured, else return it."""
    resolution = resolve_provisioned_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    assert resolution.contract is not None
    return resolution.contract
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/live_vm/test_gates.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check tests/live_vm && uv run ruff format --check tests/live_vm
uv run ty check
git add tests/live_vm/__init__.py tests/live_vm/test_gates.py
git commit -m "test(live-vm): require_live_vm_* skip/fail gates (#1290)"
```

---

### Task 4: `throwaway_domain_xml` arch builder

**Files:**
- Modify: `src/kdive/testing/live_vm.py` (add the builder)
- Modify: `tests/testing/test_live_vm.py` (add builder tests)

**Interfaces:**
- Consumes: `kdive.domain.platform.arch_traits.arch_traits`; `kdive.providers.local_libvirt.lifecycle.xml.SYSTEM_SSH_NETDEV_ID`; `kdive.providers.shared.libvirt_xml.register_qemu_namespace` + `QEMU_NS`.
- Produces: `throwaway_domain_xml(*, name: str, arch: str, disk_path: str, memory_mb: int = 1024, vcpu: int = 1, kernel_path: Path | None = None, cmdline: str | None = None, console_log: Path | None = None, ssh_hostfwd_port: int | None = None) -> str`

- [ ] **Step 1: Write the failing builder tests**

Add to `tests/testing/test_live_vm.py`:

```python
import xml.etree.ElementTree as ET

from kdive.domain.errors import CategorizedError
from kdive.testing.live_vm import throwaway_domain_xml

_QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)  # noqa: S314 - kdive-rendered, trusted


def test_builder_x86_emits_q35_ttys0_hostpassthrough_acpi() -> None:
    xml = throwaway_domain_xml(name="kdive-x", arch="x86_64", disk_path="/d.qcow2")
    root = _root(xml)
    assert root.get("type") == "kvm"
    assert root.find("./os/type").get("machine") == "q35"
    assert root.find("./cpu").get("mode") == "host-passthrough"
    assert root.find("./features/acpi") is not None
    assert root.find("./features/vmcoreinfo") is not None
    # serial is always emitted
    assert root.find("./devices/serial") is not None
    assert root.find("./devices/console") is not None


def test_builder_ppc64le_emits_pseries_hostmodel_no_acpi() -> None:
    xml = throwaway_domain_xml(name="kdive-p", arch="ppc64le", disk_path="/d.qcow2")
    root = _root(xml)
    assert root.find("./os/type").get("machine") == "pseries"
    assert root.find("./cpu").get("mode") == "host-model"
    assert root.find("./features") is None
    assert root.find("./devices/serial") is not None


def test_builder_serial_log_sink_only_when_console_log_set(tmp_path) -> None:
    without = _root(throwaway_domain_xml(name="a", arch="x86_64", disk_path="/d.qcow2"))
    assert without.find("./devices/serial/log") is None
    console = tmp_path / "c.log"
    with_log = _root(
        throwaway_domain_xml(name="b", arch="x86_64", disk_path="/d.qcow2", console_log=console)
    )
    log_el = with_log.find("./devices/serial/log")
    assert log_el is not None and log_el.get("file") == str(console)


def test_builder_ssh_netdev_present_iff_port_set() -> None:
    without = throwaway_domain_xml(name="a", arch="x86_64", disk_path="/d.qcow2")
    assert "hostfwd" not in without
    with_fwd = throwaway_domain_xml(
        name="b", arch="x86_64", disk_path="/d.qcow2", ssh_hostfwd_port=2222
    )
    assert "hostfwd=tcp:127.0.0.1:2222-:22" in with_fwd
    # q35 pins the slot; pseries does not
    assert "addr=0x10" in with_fwd
    ppc = throwaway_domain_xml(
        name="c", arch="ppc64le", disk_path="/d.qcow2", ssh_hostfwd_port=2222
    )
    assert "addr=0x10" not in ppc


def test_builder_direct_kernel_and_default_console_cmdline(tmp_path) -> None:
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"k")
    root = _root(
        throwaway_domain_xml(
            name="a", arch="x86_64", disk_path="/d.qcow2", kernel_path=kernel
        )
    )
    assert root.find("./os/kernel").text == str(kernel)
    assert root.find("./os/cmdline").text == "root=/dev/vda console=ttyS0 rw"


def test_builder_unknown_arch_raises_configuration_error() -> None:
    with pytest.raises(CategorizedError):
        throwaway_domain_xml(name="a", arch="riscv64", disk_path="/d.qcow2")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/testing/test_live_vm.py -k builder -q`
Expected: FAIL with `ImportError: cannot import name 'throwaway_domain_xml'`.

- [ ] **Step 3: Implement the builder**

Add to `src/kdive/testing/live_vm.py` (imports at top with the others):

```python
import xml.etree.ElementTree as ET

from kdive.domain.platform.arch_traits import arch_traits
from kdive.providers.local_libvirt.lifecycle.xml import SYSTEM_SSH_NETDEV_ID
from kdive.providers.shared.libvirt_xml import QEMU_NS, register_qemu_namespace

_LOOPBACK_HOST = "127.0.0.1"


def throwaway_domain_xml(
    *,
    name: str,
    arch: str,
    disk_path: str,
    memory_mb: int = 1024,
    vcpu: int = 1,
    kernel_path: Path | None = None,
    cmdline: str | None = None,
    console_log: Path | None = None,
    ssh_hostfwd_port: int | None = None,
) -> str:
    """Render a throwaway KVM domain, consuming every arch-varying fact of ``arch_traits(arch)``.

    Unlike production ``render_domain_xml`` this takes no ``ProvisioningProfile`` (a throwaway has
    no System). It emits the load-bearing ``<cpu mode>`` (ADR-0294: a missing ``<cpu>`` gives an
    EL9 guest ``qemu64``/x86-64-v1 and aborts PID 1) and the x86 ``<features>`` block so a KVM
    throwaway can boot a RHEL-family guest to userspace. Built with ElementTree — no path injects
    XML. Raises ``CONFIGURATION_ERROR`` (via ``arch_traits``) for an unknown arch.
    """
    register_qemu_namespace()
    traits = arch_traits(arch)
    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = name
    ET.SubElement(domain, "memory", unit="MiB").text = str(memory_mb)
    ET.SubElement(domain, "vcpu").text = str(vcpu)
    ET.SubElement(domain, "cpu", mode=traits.kvm_cpu_mode)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=arch, machine=traits.machine).text = "hvm"
    if kernel_path is not None:
        ET.SubElement(os_el, "kernel").text = str(kernel_path)
        resolved_cmdline = (
            cmdline if cmdline is not None else f"root=/dev/vda console={traits.console_device} rw"
        )
        ET.SubElement(os_el, "cmdline").text = resolved_cmdline
    if traits.emit_acpi_features:
        features = ET.SubElement(domain, "features")
        ET.SubElement(features, "acpi")
        ET.SubElement(features, "vmcoreinfo", state="on")
    devices = ET.SubElement(domain, "devices")
    _append_root_disk(devices, disk_path)
    _append_serial(devices, console_log)
    if ssh_hostfwd_port is not None:
        _append_ssh_netdev(domain, ssh_hostfwd_port, pin_nic_slot=traits.pin_nic_slot)
    return ET.tostring(domain, encoding="unicode")


def _append_root_disk(devices: ET.Element, disk_path: str) -> None:
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", file=disk_path)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")


def _append_serial(devices: ET.Element, console_log: Path | None) -> None:
    serial = ET.SubElement(devices, "serial", type="pty")
    if console_log is not None:
        ET.SubElement(serial, "log", file=str(console_log), append="off")
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")


def _append_ssh_netdev(domain: ET.Element, port: int, *, pin_nic_slot: bool) -> None:
    commandline = ET.SubElement(domain, f"{{{QEMU_NS}}}commandline")
    netdev = f"user,id={SYSTEM_SSH_NETDEV_ID},hostfwd=tcp:{_LOOPBACK_HOST}:{port}-:22"
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-netdev")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=netdev)
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-device")
    device = f"virtio-net-pci,netdev={SYSTEM_SSH_NETDEV_ID}"
    if pin_nic_slot:
        device = f"{device},addr=0x10"
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=device)
```

> Before implementing, confirm `QEMU_NS` and `register_qemu_namespace` are exported from `kdive.providers.shared.libvirt_xml` (grep). If the ElementTree `<os>` sub-element ordering matters to libvirt (it puts `<cpu>` after `<vcpu>` in production), keep the order shown, which mirrors `render_domain_xml`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/testing/test_live_vm.py -k builder -q`
Expected: PASS (6 builder tests).

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check src/kdive/testing tests/testing && uv run ruff format --check src/kdive/testing tests/testing
uv run ty check
git add src/kdive/testing/live_vm.py tests/testing/test_live_vm.py
git commit -m "feat(live-vm): arch-parameterized throwaway_domain_xml builder (#1290)"
```

---

### Task 5: Wait predicates, overlay + connect helpers, session-XDG handling

**Files:**
- Modify: `src/kdive/testing/live_vm.py`
- Modify: `tests/testing/test_live_vm.py`

**Interfaces:**
- Produces:
  - `wait_for_active(domain, deadline_s: float, *, sleep=time.sleep) -> bool`
  - `wait_for_panic(console_log: Path, deadline_s: float, *, sleep=time.sleep) -> bool`
  - `ssh_banner_reachable(host: str, port: int, timeout_s: float = 2.0) -> bool` (live-only, `# pragma: no cover - live_vm`)
  - `wait_for_ssh(host: str, port: int, deadline_s: float, *, probe=ssh_banner_reachable, sleep=time.sleep) -> bool`
  - `create_overlay(base: Path, dest: Path) -> None`
  - `connect_libvirt(uri: str)` (live-only, `# pragma: no cover - live_vm`)
  - `prepare_session_runtime(uri: str) -> _SessionRuntime | None` + `_SessionRuntime.restore()`

- [ ] **Step 1: Write the failing tests for the pure/injectable pieces**

Add to `tests/testing/test_live_vm.py`:

```python
from kdive.testing.live_vm import (
    prepare_session_runtime,
    wait_for_active,
    wait_for_panic,
    wait_for_ssh,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_wait_for_active_returns_true_when_domain_active() -> None:
    class _Dom:
        def isActive(self) -> bool:  # noqa: N802 - libvirt name
            return True

    assert wait_for_active(_Dom(), deadline_s=1.0) is True


def test_wait_for_panic_true_after_marker_appears(tmp_path, monkeypatch) -> None:
    console = tmp_path / "c.log"
    console.write_text("booting...\n")
    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            console.write_text("booting...\nKernel panic - not syncing\n")

    # a monotonic that advances slowly so the deadline is not hit before the marker
    assert wait_for_panic(console, deadline_s=100.0, sleep=fake_sleep) is True


def test_wait_for_panic_false_at_deadline(tmp_path) -> None:
    console = tmp_path / "c.log"
    console.write_text("no panic here\n")
    assert wait_for_panic(console, deadline_s=-1.0) is False  # already past deadline


def test_wait_for_ssh_true_when_probe_eventually_succeeds() -> None:
    seq = iter([False, False, True])

    def probe(_host: str, _port: int) -> bool:
        return next(seq)

    assert wait_for_ssh("127.0.0.1", 2222, deadline_s=100.0, probe=probe, sleep=lambda _s: None) is True


def test_wait_for_ssh_false_at_deadline_when_probe_never_succeeds() -> None:
    def probe(_host: str, _port: int) -> bool:
        return False

    assert wait_for_ssh("127.0.0.1", 2222, deadline_s=-1.0, probe=probe) is False


def test_wait_for_ssh_survives_probe_oserror() -> None:
    calls = {"n": 0}

    def probe(_host: str, _port: int) -> bool:
        calls["n"] += 1
        if calls["n"] < 2:
            raise OSError("connection refused")
        return True

    assert wait_for_ssh("127.0.0.1", 2222, deadline_s=100.0, probe=probe, sleep=lambda _s: None) is True


def test_prepare_session_runtime_none_for_system_mode(monkeypatch) -> None:
    assert prepare_session_runtime("qemu:///system") is None


def test_prepare_session_runtime_sets_short_xdg_and_restores(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/original")
    runtime = prepare_session_runtime("qemu:///session")
    assert runtime is not None
    import os

    short = os.environ["XDG_CONFIG_HOME"]
    assert short != "/original" and len(short) < 40
    runtime.restore()
    assert os.environ["XDG_CONFIG_HOME"] == "/original"
    assert not Path(short).exists()
```

> **Deadline note for `wait_for_panic`:** the implementation compares `time.monotonic()` against `time_started + deadline_s`. Passing `deadline_s=-1.0` makes the loop start already past its deadline, so it returns `False` without sleeping — a clean way to unit-test the timeout branch without a real wait. `wait_for_ssh` uses the same trick.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/testing/test_live_vm.py -k "wait_for or session_runtime" -q`
Expected: FAIL (ImportError on the new names).

- [ ] **Step 3: Implement the predicates + helpers**

Add to `src/kdive/testing/live_vm.py` (add `import contextlib`, `import socket`, `import subprocess`, `import time`, `import uuid` to the top imports):

```python
_PANIC_MARKER = "Kernel panic"
_SSH_ID_PREFIX = b"SSH-"
_POLL_INTERVAL_S = 0.5


def wait_for_active(domain, deadline_s: float, *, sleep=time.sleep) -> bool:
    """Poll ``domain.isActive()`` until true or the deadline passes."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if domain.isActive():
            return True
        sleep(_POLL_INTERVAL_S)
    return domain.isActive()


def wait_for_panic(console_log: Path, deadline_s: float, *, sleep=time.sleep) -> bool:
    """Poll the serial console file for the 'Kernel panic' marker until it appears or the deadline."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if _PANIC_MARKER in console_log.read_text(errors="replace"):
            return True
        sleep(_POLL_INTERVAL_S)
    return _PANIC_MARKER in console_log.read_text(errors="replace")


def _ssh_banner_verdict(buffer: bytes) -> bool | None:
    if buffer.startswith(_SSH_ID_PREFIX):
        return True
    if not _SSH_ID_PREFIX.startswith(buffer):
        return False
    return None


def ssh_banner_reachable(host: str, port: int, timeout_s: float = 2.0) -> bool:  # pragma: no cover - live_vm
    """One connect + sshd identification-banner read; True iff the peer speaks SSH.

    The harness owns its own probe (rather than importing the provider-internal, live-only
    _real_ssh_connect) so test-support code does not reach into provider privates.
    """
    deadline = time.monotonic() + timeout_s
    sock = socket.create_connection((host, port), timeout=timeout_s)
    buffer = b""
    try:
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                chunk = sock.recv(256)
            except TimeoutError:
                continue
            if not chunk:
                break
            buffer += chunk
            verdict = _ssh_banner_verdict(buffer)
            if verdict is not None:
                return verdict
    finally:
        sock.close()
    return False


def wait_for_ssh(
    host: str, port: int, deadline_s: float, *, probe=ssh_banner_reachable, sleep=time.sleep
) -> bool:
    """Poll ``probe(host, port)`` until it returns True or the deadline passes.

    ``probe`` is one single-shot attempt (default the real banner probe); this is the missing outer
    loop, retrying past a refused/hanging port. Injected in tests to exercise the loop without a
    live guest. ``deadline_s`` bounds the whole wait; the probe's own timeout bounds each attempt.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            if probe(host, port):
                return True
        except OSError:
            pass
        sleep(_POLL_INTERVAL_S)
    return False


def create_overlay(base: Path, dest: Path) -> None:
    """Create a qcow2 overlay at ``dest`` backed by ``base`` (staged beside it for the SELinux label)."""
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(dest)],
        check=True,
        capture_output=True,
    )


@dataclass(slots=True)
class _SessionRuntime:
    """Records the XDG_CONFIG_HOME redirect for a session-mode boot so teardown can restore it."""

    prior: str | None
    short_dir: Path

    def restore(self) -> None:
        if self.prior is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.prior
        with contextlib.suppress(OSError):
            self.short_dir.rmdir()


def prepare_session_runtime(uri: str) -> _SessionRuntime | None:
    """Redirect XDG_CONFIG_HOME to a short /tmp path for a session URI; None for system mode.

    Session-mode libvirt derives its per-domain QMP socket under $XDG_CONFIG_HOME; a deep pytest
    tmp path overflows the 108-byte UNIX socket limit. Process-global — one session boot at a time
    per process (see module docstring).
    """
    if not uri.startswith("qemu:///session"):
        return None
    prior = os.environ.get("XDG_CONFIG_HOME")
    short_dir = Path(f"/tmp/kdive-cl-{uuid.uuid4().hex[:8]}")  # noqa: S108 - short path for QMP socket
    short_dir.mkdir(parents=True, exist_ok=True)
    os.environ["XDG_CONFIG_HOME"] = str(short_dir)
    return _SessionRuntime(prior=prior, short_dir=short_dir)


def connect_libvirt(uri: str):  # pragma: no cover - live_vm
    """Open a libvirt connection. Call ``prepare_session_runtime`` first for a session URI."""
    import libvirt  # noqa: PLC0415  # operator-provided

    return libvirt.open(uri)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/testing/test_live_vm.py -k "wait_for or session_runtime" -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check src/kdive/testing tests/testing && uv run ruff format --check src/kdive/testing tests/testing
uv run ty check
git add src/kdive/testing/live_vm.py tests/testing/test_live_vm.py
git commit -m "feat(live-vm): wait predicates, overlay + session-XDG helpers (#1290)"
```

---

### Task 6: `boot_throwaway_domain` context manager

**Files:**
- Modify: `src/kdive/testing/live_vm.py`
- Modify: `tests/testing/test_live_vm.py`

**Interfaces:**
- Consumes: everything from Tasks 2/4/5.
- Produces:
  - `class LiveDomain` (frozen slotted: `name`, `domain`, `conn`, `uri`, `ssh_port: int | None`, `console_log: Path | None`)
  - `class LiveVmBootTimeout(Exception)`
  - `boot_throwaway_domain(rootfs, *, arch, name, mode="qemu:///system", memory_mb=1024, vcpu=1, ssh_hostfwd_port=None, kernel_path=None, cmdline=None, console_log=None, wait_for="active", wait_timeout_s=30.0, settle_s=0.0, _connect=connect_libvirt, _overlay=create_overlay, _sleep=time.sleep) -> Iterator[LiveDomain]`

- [ ] **Step 1: Write the failing context-manager tests (fake libvirt conn, no KVM)**

Add to `tests/testing/test_live_vm.py`:

```python
from kdive.testing.live_vm import LiveVmBootTimeout, boot_throwaway_domain


class _FakeDomain:
    def __init__(self) -> None:
        self.active = True
        self.destroyed = False
        self.undefined = False

    def isActive(self) -> bool:  # noqa: N802 - libvirt name
        return self.active

    def create(self) -> None:
        self.active = True

    def destroy(self) -> None:  # noqa: D401
        self.destroyed = True
        self.active = False

    def undefineFlags(self, _flags: int) -> None:  # noqa: N802 - libvirt name
        self.undefined = True


class _FakeConn:
    def __init__(self) -> None:
        self.domain = _FakeDomain()
        self.define_calls = 0
        self.closed = False

    def defineXML(self, _xml: str) -> _FakeDomain:  # noqa: N802 - libvirt name
        self.define_calls += 1
        return self.domain

    def close(self) -> int:
        self.closed = True
        return 0


def _fake_conn_factory(conn: _FakeConn):
    return lambda _uri: conn


def test_boot_yields_and_tears_down_in_order(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kdive.testing.live_vm.libvirt", _fake_libvirt_module(), raising=False)
    conn = _FakeConn()
    overlays: list[Path] = []
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")

    def fake_overlay(_base: Path, dest: Path) -> None:
        dest.write_bytes(b"overlay")
        overlays.append(dest)

    with boot_throwaway_domain(
        rootfs, arch="x86_64", name="kdive-t", mode="qemu:///system",
        _connect=_fake_conn_factory(conn), _overlay=fake_overlay,
    ) as live:
        assert live.name == "kdive-t"
        assert conn.define_calls == 1
        assert overlays and overlays[0].exists()
    assert conn.domain.destroyed and conn.domain.undefined and conn.closed
    assert not overlays[0].exists()  # overlay unlinked


def test_boot_raises_bemfore_define_on_ssh_without_port(tmp_path, monkeypatch) -> None:
    conn = _FakeConn()
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    with pytest.raises(Exception):  # CategorizedError
        with boot_throwaway_domain(
            rootfs, arch="x86_64", name="k", wait_for="ssh",
            _connect=_fake_conn_factory(conn), _overlay=lambda b, d: d.write_bytes(b""),
        ):
            pass
    assert conn.define_calls == 0  # failed the precondition before defining


def test_boot_timeout_raises_and_tears_down(tmp_path, monkeypatch) -> None:
    conn = _FakeConn()
    conn.domain.active = False  # never becomes active
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    with pytest.raises(LiveVmBootTimeout):
        with boot_throwaway_domain(
            rootfs, arch="x86_64", name="k", wait_timeout_s=-1.0,
            _connect=_fake_conn_factory(conn), _overlay=lambda b, d: d.write_bytes(b""),
        ):
            pass
    assert conn.closed  # teardown still ran


def test_boot_session_mode_restores_xdg_even_on_body_error(tmp_path, monkeypatch) -> None:
    import os as _os
    monkeypatch.setenv("XDG_CONFIG_HOME", "/original")
    conn = _FakeConn()
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    with pytest.raises(RuntimeError):
        with boot_throwaway_domain(
            rootfs, arch="x86_64", name="k", mode="qemu:///session",
            _connect=_fake_conn_factory(conn), _overlay=lambda b, d: d.write_bytes(b""),
        ):
            raise RuntimeError("body boom")
    assert _os.environ["XDG_CONFIG_HOME"] == "/original"
```

> The `_fake_libvirt_module()` helper stubs the module-level `libvirt` name the teardown references for `libvirt.libvirtError` and `libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA`. Define it in the test:
> ```python
> def _fake_libvirt_module():
>     import types
>     mod = types.SimpleNamespace()
>     mod.libvirtError = Exception
>     mod.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA = 1
>     return mod
> ```
> The implementation imports `libvirt` lazily *inside* `boot_throwaway_domain` for the teardown suppression; the test injects the stub via `monkeypatch.setattr` on the module attribute, or — cleaner — the implementation accepts the caught exception type through a small internal indirection. Simplest: teardown does `import libvirt` lazily and the test sets `monkeypatch.setitem(sys.modules, "libvirt", _fake_libvirt_module())` before entering. Use whichever keeps the teardown real; verify the fake path in step 4.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/testing/test_live_vm.py -k boot -q`
Expected: FAIL (ImportError on `boot_throwaway_domain`).

- [ ] **Step 3: Implement the context manager**

Add to `src/kdive/testing/live_vm.py` (`import contextlib`, `from collections.abc import Iterator`, `from contextlib import contextmanager` at top; `CategorizedError`/`ErrorCategory` from `kdive.domain.errors`):

```python
_VALID_WAITS = ("active", "panic", "ssh")


@dataclass(frozen=True, slots=True)
class LiveDomain:
    name: str
    domain: object
    conn: object
    uri: str
    ssh_port: int | None
    console_log: Path | None


class LiveVmBootTimeout(Exception):
    """A throwaway domain did not reach its wait condition before the deadline."""


def _validate_wait(wait_for: str, *, ssh_hostfwd_port: int | None, console_log: Path | None) -> None:
    if wait_for not in _VALID_WAITS:
        raise CategorizedError(
            f"unknown wait_for {wait_for!r}; expected one of {_VALID_WAITS}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if wait_for == "ssh" and ssh_hostfwd_port is None:
        raise CategorizedError(
            'wait_for="ssh" requires ssh_hostfwd_port so the probe has a port to reach',
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if wait_for == "panic" and console_log is None:
        raise CategorizedError(
            'wait_for="panic" requires console_log so the panic-wait can read the serial console',
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def _await_condition(
    wait_for: str, domain, *, deadline_s: float, ssh_port: int | None, console_log: Path | None
) -> bool:
    if wait_for == "active":
        return wait_for_active(domain, deadline_s)
    if wait_for == "panic":
        assert console_log is not None
        return wait_for_panic(console_log, deadline_s)
    assert ssh_port is not None
    return wait_for_ssh(_LOOPBACK_HOST, ssh_port, deadline_s)


@contextmanager
def boot_throwaway_domain(
    rootfs: Path,
    *,
    arch: str,
    name: str,
    mode: str = "qemu:///system",
    memory_mb: int = 1024,
    vcpu: int = 1,
    ssh_hostfwd_port: int | None = None,
    kernel_path: Path | None = None,
    cmdline: str | None = None,
    console_log: Path | None = None,
    wait_for: str = "active",
    wait_timeout_s: float = 30.0,
    settle_s: float = 0.0,
    _connect=connect_libvirt,
    _overlay=create_overlay,
    _sleep=time.sleep,
) -> Iterator[LiveDomain]:
    """Boot a throwaway libvirt domain, wait for ``wait_for``, yield it, and guarantee teardown.

    See the module docstring for the environment contract. ``settle_s`` sleeps after the condition
    is reached (preserves the legacy ``create(); sleep(2)`` window). ``_connect``/``_overlay``/
    ``_sleep`` are injection seams for the unit tests; live callers use the defaults.
    """
    import libvirt  # noqa: PLC0415  # operator-provided; teardown suppresses its libvirtError

    _validate_wait(wait_for, ssh_hostfwd_port=ssh_hostfwd_port, console_log=console_log)
    dest = rootfs.with_name(f"{name}.qcow2")
    runtime = prepare_session_runtime(mode)
    conn = None
    domain = None
    try:
        _overlay(rootfs, dest)
        conn = _connect(mode)
        xml = throwaway_domain_xml(
            name=name,
            arch=arch,
            disk_path=str(dest),
            memory_mb=memory_mb,
            vcpu=vcpu,
            kernel_path=kernel_path,
            cmdline=cmdline,
            console_log=console_log,
            ssh_hostfwd_port=ssh_hostfwd_port,
        )
        domain = conn.defineXML(xml)
        domain.create()
        if not _await_condition(
            wait_for, domain, deadline_s=wait_timeout_s, ssh_port=ssh_hostfwd_port,
            console_log=console_log,
        ):
            raise LiveVmBootTimeout(
                f"domain {name!r} (mode {mode}) did not reach wait_for={wait_for!r} in "
                f"{wait_timeout_s}s"
            )
        if settle_s > 0:
            _sleep(settle_s)
        yield LiveDomain(
            name=name, domain=domain, conn=conn, uri=mode,
            ssh_port=ssh_hostfwd_port, console_log=console_log,
        )
    finally:
        if domain is not None:
            with contextlib.suppress(libvirt.libvirtError):
                if domain.isActive():
                    domain.destroy()
            with contextlib.suppress(libvirt.libvirtError):
                domain.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
        if runtime is not None:
            runtime.restore()
```

> The lazy `import libvirt` at the top of the function is what the unit tests stub via `sys.modules`. Confirm in step 4 the injected fake makes the teardown branch run without a real libvirt. If stubbing `sys.modules["libvirt"]` proves awkward, an acceptable alternative is a module-level `import libvirt` guarded so the tests patch `kdive.testing.live_vm.libvirt`; keep the teardown suppression real either way.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/testing/test_live_vm.py -k boot -q`
Expected: PASS (fix the deliberate `bemfore` typo in the test name if copied — rename to `test_boot_raises_before_define_on_ssh_without_port`).

- [ ] **Step 5: Full mechanism suite + guardrails + commit**

```bash
uv run python -m pytest tests/testing/test_live_vm.py -q
uv run ruff check src/kdive/testing tests/testing && uv run ruff format --check src/kdive/testing tests/testing
uv run ty check
git add src/kdive/testing/live_vm.py tests/testing/test_live_vm.py
git commit -m "feat(live-vm): boot_throwaway_domain context manager (#1290)"
```

---

### Task 7: Migrate `test_snapshot_live.py` onto the harness

**Files:**
- Modify: `tests/providers/local_libvirt/test_snapshot_live.py`

**Interfaces:**
- Consumes: `boot_throwaway_domain`, `require_live_vm_throwaway`.

- [ ] **Step 1: Rewrite the test body onto the harness**

Replace the boot/overlay/teardown boilerplate (current lines ~23-101) while keeping every snapshot assertion. The new shape:

```python
import uuid
from pathlib import Path

import pytest

from kdive.testing.live_vm import boot_throwaway_domain
from tests.live_vm import require_live_vm_throwaway


@pytest.mark.live_vm
@pytest.mark.live_vm_throwaway
def test_live_vm_snapshotter_create_revert_resume_delete() -> None:  # pragma: no cover - live_vm
    contract = require_live_vm_throwaway("qemu:///system")
    try:
        import libvirt  # noqa: PLC0415  # operator-provided
    except ImportError:
        pytest.skip("libvirt-python unavailable")

    from kdive.providers.local_libvirt.lifecycle.snapshot import (  # noqa: PLC0415
        LocalLibvirtSnapshotter,
    )

    name = f"kdive-snap-live-{uuid.uuid4().hex[:12]}"
    snapshotter = LocalLibvirtSnapshotter(connect=lambda: libvirt.open(contract.libvirt_uri))
    with boot_throwaway_domain(
        contract.rootfs, arch="x86_64", name=name, mode=contract.libvirt_uri, settle_s=2.0
    ) as live:
        dom = live.domain
        snapshotter.create(name, "cp1", include_memory=True)
        assert "cp1" in {snap.getName() for snap in dom.listAllSnapshots(0)}

        snapshotter.revert(name, "cp1", start_paused=False)
        assert dom.isActive()

        snapshotter.revert(name, "cp1", start_paused=True)
        assert dom.state()[0] == libvirt.VIR_DOMAIN_PAUSED

        snapshotter.delete(name, "cp1")
        assert "cp1" not in {snap.getName() for snap in dom.listAllSnapshots(0)}

        snapshotter.create(name, "disk-a", include_memory=False)
        snapshotter.create(name, "disk-b", include_memory=False)
        snapshotter.delete_all(name)
        assert dom.listAllSnapshots(0) == []
```

> Keep the module docstring; update it to note the harness (`boot_throwaway_domain`) and the additive `live_vm_throwaway` marker. The snapshot cleanup that the old `finally` did (`snapshotter.delete_all`) is now redundant with the harness's `undefineFlags(...SNAPSHOTS_METADATA)` teardown — drop the manual snapshot cleanup; if a leftover-snapshot risk is real, wrap the body's snapshot ops so a failure still lets the harness undefine (it always will, in `finally`).

- [ ] **Step 2: Static-check the migrated file (no live host needed)**

Run: `uv run ruff check tests/providers/local_libvirt/test_snapshot_live.py && uv run ruff format --check tests/providers/local_libvirt/test_snapshot_live.py`
Run: `uv run ty check`
Run: `uv run python -m pytest tests/providers/local_libvirt/test_snapshot_live.py --collect-only -q`
Expected: lint/type clean; collection shows the test with `live_vm` + `live_vm_throwaway` markers and it is deselected under the default `-m "not live_vm"`.

- [ ] **Step 3: Additivity guard still green**

Run: `uv run python -m pytest tests/live_vm/test_family_markers.py -q`
Expected: PASS (the migrated test carries both markers).

- [ ] **Step 4: Commit**

```bash
git add tests/providers/local_libvirt/test_snapshot_live.py
git commit -m "test(live-vm): migrate snapshot live test onto boot_throwaway_domain (#1290)"
```

---

### Task 8: Migrate `test_traffic_capture_live.py` onto the harness

**Files:**
- Modify: `tests/providers/local_libvirt/test_traffic_capture_live.py`

**Interfaces:**
- Consumes: `boot_throwaway_domain`, `require_live_vm_throwaway` (with `session_required=True`).

- [ ] **Step 1: Rewrite onto the harness, session-required + ssh_hostfwd_port + settle**

Keep the free-port helper, the filter-dump attach/detach, and the pcap assertion. Replace the boot/overlay/XDG/teardown with:

```python
    contract = require_live_vm_throwaway("qemu:///session", session_required=True)
    ...
    name = f"kdive-cap-live-{uuid.uuid4().hex[:12]}"
    port = _free_port()
    ...
    capturer = LocalLibvirtTrafficCapture(
        connect=lambda: libvirt.open(contract.libvirt_uri), monitor=libvirt_qemu.qemuMonitorCommand
    )
    with boot_throwaway_domain(
        contract.rootfs, arch="x86_64", name=name, mode=contract.libvirt_uri,
        ssh_hostfwd_port=port, wait_for="active", settle_s=2.0,
    ) as live:
        capturer.attach(name, qom_id=qom_id, dest_path=str(pcap_file), snaplen=128)
        for _ in range(8):
            with (
                contextlib.suppress(OSError),
                socket.create_connection(("127.0.0.1", port), timeout=1) as sock,
            ):
                sock.sendall(b"kdive-capture-probe\n")
            time.sleep(0.2)
        capturer.detach(name, qom_id=qom_id)
        data = pcap_file.read_bytes()
        assert count_pcap_packets(data) > 0, "filter-dump captured no packets"
    # pcap_file / pcap_dir cleanup stays in this test (the harness owns only the domain + overlay)
```

Mark the test `@pytest.mark.live_vm` + `@pytest.mark.live_vm_throwaway`. Delete the now-dead inline `domain_xml`, the manual `XDG_CONFIG_HOME` short-path block (the harness owns it via `prepare_session_runtime`), and the `defineXML`/`create`/`finally` domain teardown. The pcap file/dir cleanup that is not the harness's concern stays (wrap it in a `finally` around the `with` or use `tempfile` context).

> The harness stages the overlay **beside `contract.rootfs`** (same as the old `Path(rootfs).with_name(...)`), so the SLIRP netdev + filter-dump path is unchanged. `settle_s=2.0` preserves the old `sleep(2)` before the attach.

- [ ] **Step 2: Static-check + collect**

Run: `uv run ruff check tests/providers/local_libvirt/test_traffic_capture_live.py && uv run ruff format --check tests/providers/local_libvirt/test_traffic_capture_live.py`
Run: `uv run ty check`
Run: `uv run python -m pytest tests/providers/local_libvirt/test_traffic_capture_live.py --collect-only -q`
Expected: clean; both markers present.

- [ ] **Step 3: Commit**

```bash
git add tests/providers/local_libvirt/test_traffic_capture_live.py
git commit -m "test(live-vm): migrate traffic-capture live test onto boot_throwaway_domain (#1290)"
```

---

### Task 9: Full guardrail suite + live proof

**Files:** none (verification).

- [ ] **Step 1: Full CI gate**

Run: `just ci`
Expected: green (lint, type whole-tree, lint-shell, lint-workflows, check-mermaid, test). Fix any drift (a generated-doc or cross-cutting guard) before proceeding — `just test` alone can miss these.

- [ ] **Step 2: Live proof on this KVM host**

Prereq: export `KDIVE_LIVE_VM_ROOTFS` at a bootable kdive-ready qcow2 in a writable, `virt_image_t`-labeled dir (see `docs/operating` / the local-libvirt walkthrough). Then:

Run: `just test-live -k "snapshotter_create_revert_resume_delete or traffic_capture_filter_dump"` (or the full `just test-live` if the host is fully provisioned).
Expected: both migrated tests PASS against real libvirt — the proof the harness is faithful. If a test errors on env (e.g. non-writable dir), the gate fails loud with an actionable message (by design); fix the env, not the test.

- [ ] **Step 3: Record the live result**

Note in the PR description which tests were live-run and their outcome (green), plus the host arch (x86_64 KVM). This is the sub-issue A acceptance evidence.

- [ ] **Step 4: Final commit if any guardrail fixes were needed**

```bash
git add <explicit paths>
git commit -m "chore(live-vm): guardrail fixes for the live_vm harness (#1290)"
```

---

## Self-Review

**Spec coverage:**
- `boot_throwaway_domain` (mechanism, teardown, settle, wait dispatch) → Task 6. ✓
- `throwaway_domain_xml` (arch traits incl. `<cpu>`/features/serial/netdev/kernel) → Task 4. ✓
- `connect_libvirt` + session-XDG save/restore → Task 5 + Task 6 (round-trip). ✓
- `create_overlay` → Task 5. ✓
- Three wait predicates + injectable `wait_for_ssh` → Task 5. ✓
- Env resolvers (ABSENT/MISCONFIGURED incl. dir-writability; symmetric `default_uri`) → Task 2. ✓
- `require_live_vm_*` gates + `session_required` → Task 3. ✓
- Family sub-markers registered + additivity meta-test → Task 1. ✓
- `wait_for` precondition guards (fail before define) → Task 6. ✓
- Dogfood migration of both throwaway tests, markers, `settle_s=2.0` → Tasks 7–8. ✓
- Contract documented in module docstring → Task 2. ✓
- Concurrency contract (one session boot/process) → Task 2 docstring + Task 5. ✓
- Live proof → Task 9. ✓

**Placeholder scan:** the `KDIVE_S3_*` names and the `QEMU_NS`/`register_qemu_namespace` exports are the only "confirm before implementing" notes — both are concrete lookups with a named grep, not open-ended TODOs. No "add error handling"/"similar to Task N" placeholders.

**Type consistency:** `EnvResolution[T].contract`/`.state`/`.reason`, `ThrowawayContract.libvirt_uri`/`.rootfs`, `LiveDomain` fields, and `boot_throwaway_domain`'s signature are used identically across Tasks 2–8. `require_live_vm_throwaway(default_uri, *, session_required)` matches between Task 3's definition and Tasks 7–8's calls.
