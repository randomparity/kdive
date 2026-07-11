# Debug-feature kernel-config advertise + gate — Implementation Plan (#1052)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Advertise each debug feature's required `CONFIG_*` symbols to the agent (advisory) and refuse to arm the two Run-addressed config-dependent seams (kdump crashkernel reservation, kdump-method vmcore) when the uploaded `effective_config` provably lacks the required symbols.

**Architecture:** A new pure `src/kdive/kernel_config/` package holds a feature→symbol registry (each feature carrying an advertised superset and a narrower `gate_required` subset of OR-group clauses), a `.config` parser, a support check, and a fail-open read helper for the Run-owned `effective_config` artifact. A static `artifacts.feature_config_requirements` MCP tool advertises the manifest; two seam handlers call the gate before arming. No schema change.

**Tech Stack:** Python 3.14, `uv`, psycopg3 (async), FastMCP, pytest. Spec: `docs/superpowers/specs/2026-07-08-debug-feature-config-gate-1052-design.md`. ADR: `docs/adr/0318-debug-feature-config-gate.md`.

## Global Constraints

- **Branch:** `feat/advertise-gate-kernel-config-1052`; **BASE_BRANCH:** `main`.
- **Guardrails (run before every commit):** `just lint` (ruff check + format), `just type` (ty, **whole tree** src+tests), `just test` (excludes `live_vm`). Full gate: `just ci`. Doc regen: `just docs` (mutating) / `just docs-check` (CI gate).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Google-style docstrings on non-trivial public APIs. Absolute imports only (`from kdive....`), no relative imports.
- `ty` runs strict; unstubbed C-ext deps get scoped per-site ignores (not relevant here).
- Error taxonomy: `kdive.domain.errors.ErrorCategory` — reuse `CONFIGURATION_ERROR`; never invent strings.
- Doc-style: no "critical/crucial/essential/significant/comprehensive/robust/elegant"; "Milestone" not "Sprint".
- The `effective_config` artifact is `Sensitivity.SENSITIVE`: never echo its bytes into a response — only booleans and `CONFIG_*` symbol names (public knowledge) may leave the seam.
- **Fail-open is mandatory** at the gate: a config read that errors, is absent, or is degenerate (zero enabled symbols) must arm-as-today, never fail the arming action.

---

## File Structure

- `src/kdive/kernel_config/__init__.py` — package marker + public re-exports.
- `src/kdive/kernel_config/requirements.py` — `Clause`, `FeatureRequirement`, the `FEATURE_REQUIREMENTS` registry, feature-id constants, `feature_requirement(id)`, `feature_manifest()`.
- `src/kdive/kernel_config/parse.py` — `KernelConfig`, `parse_kernel_config(bytes) -> KernelConfig`.
- `src/kdive/kernel_config/support.py` — `unmet_clauses`, `feature_supported`, `missing_symbols`.
- `src/kdive/kernel_config/fetch.py` — `load_effective_config(conn, run_id, *, store_factory) -> KernelConfig | None` (fail-open).
- `src/kdive/mcp/tools/catalog/artifacts/feature_requirements.py` — `feature_config_requirements() -> ToolResponse` + `FEATURE_CONFIG_REQUIREMENTS_TOOL`.
- Modified: `src/kdive/mcp/tools/catalog/artifacts/registrar.py` (register the tool), `src/kdive/jobs/handlers/runs/install.py` (crash gate), `src/kdive/mcp/tools/lifecycle/vmcore_handlers.py` (vmcore gate), `src/kdive/jobs/handlers/diagnostic_sysrq.py` (remediation text), and the `runs.create` / `artifacts.expected_uploads` next-actions.
- Tests mirror under `tests/kernel_config/`, `tests/mcp/catalog/`, `tests/jobs/handlers/`, `tests/mcp/lifecycle/`.

---

## Task 1: `kernel_config` registry

**Files:**
- Create: `src/kdive/kernel_config/__init__.py`, `src/kdive/kernel_config/requirements.py`
- Test: `tests/kernel_config/__init__.py`, `tests/kernel_config/test_requirements.py`

**Interfaces:**
- Produces: `Clause = frozenset[str]`; `FeatureRequirement` (frozen dataclass: `feature: str`, `summary: str`, `advertised: tuple[Clause, ...]`, `gate_required: tuple[Clause, ...]`, property `gated: bool`); `FEATURE_REQUIREMENTS: tuple[FeatureRequirement, ...]`; constants `CRASH_CAPTURE = "crash_capture"`, `SYSRQ = "sysrq"`; `feature_requirement(feature_id: str) -> FeatureRequirement` (raises `KeyError` on unknown); `feature_manifest() -> list[dict]` (JSON-ready).

- [ ] **Step 1: Write the failing test**

```python
# tests/kernel_config/test_requirements.py
from kdive.kernel_config.requirements import (
    CRASH_CAPTURE,
    FEATURE_REQUIREMENTS,
    SYSRQ,
    feature_manifest,
    feature_requirement,
)


def test_crash_capture_gate_excludes_kaslr_and_or_groups_kexec():
    feat = feature_requirement(CRASH_CAPTURE)
    gate_symbols = {s for clause in feat.gate_required for s in clause}
    assert "RANDOMIZE_BASE" not in gate_symbols  # KASLR advertised-only
    assert "RANDOMIZE_BASE" in {s for clause in feat.advertised for s in clause}
    assert frozenset({"KEXEC", "KEXEC_FILE"}) in feat.gate_required  # either load syscall
    assert feat.gated is True


def test_advertise_only_features_have_empty_gate_required():
    for fid in ("rootfs_mount", "ikconfig", "debuginfo", "kasan", "serial_console"):
        feat = feature_requirement(fid)
        assert feat.gate_required == ()
        assert feat.gated is False


def test_sysrq_is_advertised_and_gate_required_magic_sysrq():
    feat = feature_requirement(SYSRQ)
    assert feat.gate_required == (frozenset({"MAGIC_SYSRQ"}),)


def test_manifest_covers_every_feature_and_exposes_advertised_not_gate_required():
    manifest = feature_manifest()
    assert {m["feature"] for m in manifest} == {f.feature for f in FEATURE_REQUIREMENTS}
    entry = next(m for m in manifest if m["feature"] == CRASH_CAPTURE)
    assert entry["gated"] is True
    assert entry["summary"]
    # requirements is the ADVERTISED superset, as a list of OR-groups (lists of symbols)
    flat = {s for group in entry["requirements"] for s in group}
    assert "RANDOMIZE_BASE" in flat
    assert "gate_required" not in entry  # internal, not advertised


def test_unknown_feature_raises():
    import pytest

    with pytest.raises(KeyError):
        feature_requirement("does_not_exist")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/kernel_config/test_requirements.py -q`
Expected: FAIL (ModuleNotFoundError: kdive.kernel_config).

- [ ] **Step 3: Write the implementation**

```python
# src/kdive/kernel_config/__init__.py
"""Advisory kernel-config feature requirements: advertise + gate (ADR-0318, #1052)."""
```

```python
# src/kdive/kernel_config/requirements.py
"""Feature -> required CONFIG_* registry (ADR-0318).

Single source of truth for both the advertised manifest and the arming gate. Each feature
carries an ``advertised`` superset (guidance shown to the agent) and a deliberately narrower
``gate_required`` subset (what the gate refuses on). Each clause is an OR-group: satisfied
when any member symbol is enabled. Symbol names are bare (no ``CONFIG_`` prefix), matching
:func:`kdive.kernel_config.parse.parse_kernel_config`.
"""

from __future__ import annotations

from dataclasses import dataclass

Clause = frozenset[str]

CRASH_CAPTURE = "crash_capture"
SYSRQ = "sysrq"


@dataclass(frozen=True, slots=True)
class FeatureRequirement:
    """One debug/platform feature and the kernel symbols it wants.

    ``advertised`` is the full recommended set (manifest guidance); ``gate_required`` is the
    minimal subset the gate refuses on (``()`` = advertise-only, never gated). Both are ordered
    tuples of OR-group clauses.
    """

    feature: str
    summary: str
    advertised: tuple[Clause, ...]
    gate_required: tuple[Clause, ...] = ()

    @property
    def gated(self) -> bool:
        """True when this feature has any hard-required symbols (is gate-enforced)."""
        return bool(self.gate_required)


def _plain(*symbols: str) -> tuple[Clause, ...]:
    """Each symbol as its own single-member OR-group clause."""
    return tuple(frozenset({s}) for s in symbols)


FEATURE_REQUIREMENTS: tuple[FeatureRequirement, ...] = (
    FeatureRequirement(
        "rootfs_mount",
        "Mount the kdive squashfs+overlay rootfs the guest boots from.",
        _plain("SQUASHFS", "SQUASHFS_ZSTD", "OVERLAY_FS", "BLK_DEV_LOOP", "XFS_FS", "XFS_POSIX_ACL"),
    ),
    FeatureRequirement(
        CRASH_CAPTURE,
        "Reserve a crashkernel and capture a vmcore via kdump.",
        _plain(
            "KEXEC", "KEXEC_CORE", "KEXEC_FILE", "CRASH_DUMP", "VMCORE_INFO",
            "PROC_VMCORE", "FW_CFG_SYSFS", "RELOCATABLE", "RANDOMIZE_BASE",
        ),
        gate_required=(
            frozenset({"KEXEC_CORE"}),
            frozenset({"KEXEC", "KEXEC_FILE"}),  # either load syscall suffices
            frozenset({"CRASH_DUMP"}),
            frozenset({"PROC_VMCORE"}),
            frozenset({"VMCORE_INFO"}),
            frozenset({"FW_CFG_SYSFS"}),
            frozenset({"RELOCATABLE"}),
        ),
    ),
    FeatureRequirement(
        "ikconfig",
        "Read the running kernel's own config back via /proc/config.gz.",
        _plain("IKCONFIG", "IKCONFIG_PROC"),
    ),
    FeatureRequirement(
        "debuginfo",
        "Resolve symbols for gdb/drgn debugging (build with DWARF or BTF).",
        (
            frozenset({"DEBUG_INFO"}),
            frozenset({"DEBUG_INFO_DWARF5", "DEBUG_INFO_DWARF4", "DEBUG_INFO_BTF"}),
            frozenset({"DEBUG_KERNEL"}),
        ),
    ),
    FeatureRequirement(
        SYSRQ,
        "Inject magic SysRq diagnostics from the host.",
        _plain("MAGIC_SYSRQ"),
        gate_required=(frozenset({"MAGIC_SYSRQ"}),),
    ),
    FeatureRequirement(
        "kasan",
        "Kernel Address Sanitizer instrumentation.",
        _plain("KASAN", "KASAN_INLINE"),
    ),
    FeatureRequirement(
        "serial_console",
        "Serial console + virtio devices the local-libvirt profile expects.",
        _plain("SERIAL_8250_CONSOLE", "VIRTIO_BLK", "VIRTIO_PCI"),
    ),
)

_BY_ID: dict[str, FeatureRequirement] = {f.feature: f for f in FEATURE_REQUIREMENTS}


def feature_requirement(feature_id: str) -> FeatureRequirement:
    """Return the registry entry for ``feature_id`` (raises ``KeyError`` if unknown)."""
    return _BY_ID[feature_id]


def feature_manifest() -> list[dict[str, object]]:
    """Render the advertised manifest (advisory): one entry per feature, ``advertised`` only."""
    return [
        {
            "feature": f.feature,
            "summary": f.summary,
            "gated": f.gated,
            "requirements": [sorted(clause) for clause in f.advertised],
        }
        for f in FEATURE_REQUIREMENTS
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/kernel_config/test_requirements.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/kernel_config/__init__.py src/kdive/kernel_config/requirements.py tests/kernel_config/
git commit -m "feat(kernel-config): feature -> CONFIG_* requirement registry (#1052)"
```

---

## Task 2: `.config` parser

**Files:**
- Create: `src/kdive/kernel_config/parse.py`
- Test: `tests/kernel_config/test_parse.py`

**Interfaces:**
- Produces: `KernelConfig` (frozen dataclass: `enabled: frozenset[str]`; method `is_enabled(symbol: str) -> bool`; property `is_degenerate: bool` == `not self.enabled`); `parse_kernel_config(data: bytes) -> KernelConfig`.
- A symbol is enabled iff a line `CONFIG_<SYM>=y` or `CONFIG_<SYM>=m` appears; `# CONFIG_<SYM> is not set`, absence, and other values (`=n`, strings, ints) are not enabled. Stored bare (no `CONFIG_` prefix).

- [ ] **Step 1: Write the failing test**

```python
# tests/kernel_config/test_parse.py
from kdive.kernel_config.parse import parse_kernel_config

_SAMPLE = b"""# Automatically generated file
CONFIG_KEXEC=y
CONFIG_KEXEC_FILE=y
CONFIG_MAGIC_SYSRQ=m
# CONFIG_RANDOMIZE_BASE is not set
CONFIG_LOCALVERSION="-kdive"
CONFIG_NR_CPUS=8

garbage line that is not a config
CONFIG_KASAN=n
"""


def test_y_and_m_are_enabled():
    cfg = parse_kernel_config(_SAMPLE)
    assert cfg.is_enabled("KEXEC")
    assert cfg.is_enabled("KEXEC_FILE")
    assert cfg.is_enabled("MAGIC_SYSRQ")  # =m counts


def test_not_set_absent_and_n_are_disabled():
    cfg = parse_kernel_config(_SAMPLE)
    assert not cfg.is_enabled("RANDOMIZE_BASE")  # is not set
    assert not cfg.is_enabled("KASAN")           # =n
    assert not cfg.is_enabled("CRASH_DUMP")      # absent


def test_string_and_int_values_are_not_enabled():
    cfg = parse_kernel_config(_SAMPLE)
    assert not cfg.is_enabled("LOCALVERSION")
    assert not cfg.is_enabled("NR_CPUS")


def test_bare_symbol_names_no_config_prefix():
    cfg = parse_kernel_config(_SAMPLE)
    assert "KEXEC" in cfg.enabled
    assert "CONFIG_KEXEC" not in cfg.enabled


def test_empty_and_non_utf8_are_degenerate_not_crash():
    assert parse_kernel_config(b"").is_degenerate
    assert parse_kernel_config(b"\xff\xfe not a config").is_degenerate
    assert not parse_kernel_config(_SAMPLE).is_degenerate
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/kernel_config/test_parse.py -q`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Write the implementation**

```python
# src/kdive/kernel_config/parse.py
"""Parse a Linux kernel ``.config`` into the set of enabled symbols (ADR-0318).

Pure and tolerant: a malformed / truncated / non-config upload yields a degenerate (empty)
result rather than raising, so the gate can fail open on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# CONFIG_<SYM>=y or =m -> enabled. Anything else (=n, "string", 123, "is not set") -> not.
_ENABLED = re.compile(r"^CONFIG_([A-Z0-9_]+)=(y|m)\s*$")


@dataclass(frozen=True, slots=True)
class KernelConfig:
    """The set of enabled kernel symbols (bare, no ``CONFIG_`` prefix)."""

    enabled: frozenset[str]

    def is_enabled(self, symbol: str) -> bool:
        """True when ``symbol`` (bare name) is built in (``=y``) or a module (``=m``)."""
        return symbol in self.enabled

    @property
    def is_degenerate(self) -> bool:
        """True when no symbols are enabled — signals a non-authoritative/empty upload."""
        return not self.enabled


def parse_kernel_config(data: bytes) -> KernelConfig:
    """Parse ``.config`` bytes into a :class:`KernelConfig` of enabled symbols."""
    text = data.decode("utf-8", "replace")
    enabled = {m.group(1) for line in text.splitlines() if (m := _ENABLED.match(line.strip()))}
    return KernelConfig(enabled=frozenset(enabled))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/kernel_config/test_parse.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/kernel_config/parse.py tests/kernel_config/test_parse.py
git commit -m "feat(kernel-config): tolerant .config parser (#1052)"
```

---

## Task 3: Support check

**Files:**
- Create: `src/kdive/kernel_config/support.py`
- Test: `tests/kernel_config/test_support.py`

**Interfaces:**
- Consumes: `KernelConfig` (Task 2), `FeatureRequirement`/`feature_requirement` (Task 1).
- Produces: `unmet_clauses(config: KernelConfig, feature: FeatureRequirement) -> tuple[Clause, ...]` (the `gate_required` OR-groups with no enabled member); `feature_supported(config, feature) -> bool` (== no unmet clauses); `missing_symbols(unmet: tuple[Clause, ...]) -> list[str]` (sorted flat symbol list for a refusal reason).

- [ ] **Step 1: Write the failing test**

```python
# tests/kernel_config/test_support.py
from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import CRASH_CAPTURE, feature_requirement
from kdive.kernel_config.support import feature_supported, missing_symbols, unmet_clauses

_CRASH = feature_requirement(CRASH_CAPTURE)
_FULL = frozenset({"KEXEC_CORE", "KEXEC", "CRASH_DUMP", "PROC_VMCORE", "VMCORE_INFO",
                   "FW_CFG_SYSFS", "RELOCATABLE"})


def test_kaslr_off_full_gate_set_is_supported():
    # RANDOMIZE_BASE absent but every gate_required clause met -> supported.
    assert feature_supported(KernelConfig(_FULL), _CRASH) is True
    assert unmet_clauses(KernelConfig(_FULL), _CRASH) == ()


def test_kexec_or_group_satisfied_by_either_syscall():
    only_file = (_FULL - {"KEXEC"}) | {"KEXEC_FILE"}
    assert feature_supported(KernelConfig(frozenset(only_file)), _CRASH) is True


def test_missing_one_clause_is_unsupported_and_named():
    cfg = KernelConfig(_FULL - {"PROC_VMCORE"})
    unmet = unmet_clauses(cfg, _CRASH)
    assert feature_supported(cfg, _CRASH) is False
    assert missing_symbols(unmet) == ["PROC_VMCORE"]


def test_missing_both_kexec_syscalls_names_both():
    cfg = KernelConfig(_FULL - {"KEXEC"})  # neither KEXEC nor KEXEC_FILE
    unmet = unmet_clauses(cfg, _CRASH)
    assert missing_symbols(unmet) == ["KEXEC", "KEXEC_FILE"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/kernel_config/test_support.py -q`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Write the implementation**

```python
# src/kdive/kernel_config/support.py
"""Check a parsed kernel config against a feature's gate_required clauses (ADR-0318)."""

from __future__ import annotations

from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import Clause, FeatureRequirement


def unmet_clauses(config: KernelConfig, feature: FeatureRequirement) -> tuple[Clause, ...]:
    """The ``gate_required`` OR-groups with no enabled member (empty tuple = fully supported)."""
    return tuple(
        clause
        for clause in feature.gate_required
        if not any(config.is_enabled(symbol) for symbol in clause)
    )


def feature_supported(config: KernelConfig, feature: FeatureRequirement) -> bool:
    """True when every ``gate_required`` clause is satisfied by ``config``."""
    return not unmet_clauses(config, feature)


def missing_symbols(unmet: tuple[Clause, ...]) -> list[str]:
    """Flatten unmet clauses into a sorted symbol list for a refusal reason."""
    return sorted({symbol for clause in unmet for symbol in clause})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/kernel_config/test_support.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/kernel_config/support.py tests/kernel_config/test_support.py
git commit -m "feat(kernel-config): gate_required support check (#1052)"
```

---

## Task 4: Fail-open effective_config reader

**Files:**
- Create: `src/kdive/kernel_config/fetch.py`
- Test: `tests/kernel_config/test_fetch.py`

**Interfaces:**
- Consumes: `parse_kernel_config` (Task 2); `FetchedArtifact` (`kdive.artifacts.storage`, has `.data: bytes`); `object_store_from_env` (`kdive.store.objectstore`).
- Produces: a Protocol `ConfigStore` with `get_artifact(key: str, etag: str | None) -> FetchedArtifact`; `async load_effective_config(conn: AsyncConnection, run_id: UUID, *, store_factory: Callable[[], ConfigStore] = object_store_from_env) -> KernelConfig | None`. Returns `None` (arm-as-today) for: no artifact row, any DB/store error, or a degenerate parse. Never raises.

- [ ] **Step 1: Write the failing test**

> **Async-test convention (verified):** this repo has **no** pytest-asyncio/anyio plugin (`pyproject.toml [tool.pytest.ini_options]` sets no `asyncio_mode`; there are zero `pytest.mark.asyncio` tests). Async code is tested with a **sync** `def test_*()` that wraps the coroutine in `asyncio.run(_impl())` — see `tests/jobs/handlers/test_diagnostic_sysrq_handler.py:157,175,209`. Follow that convention exactly; a bare `async def test_*` would be silently uncollected (false green).

```python
# tests/kernel_config/test_fetch.py
import asyncio
from uuid import uuid4

from kdive.artifacts.storage import FetchedArtifact
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.sensitivity import Sensitivity  # confirm module: `rg -n "class Sensitivity" src/kdive`
from kdive.kernel_config.fetch import load_effective_config

_GOOD = b"CONFIG_KEXEC=y\nCONFIG_PROC_VMCORE=y\n"


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params):
        self._executed = (sql, params)

    async def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self, *, row_factory):
        return _FakeCursor(self._row)


class _Store:
    def __init__(self, data=None, exc=None):
        self._data, self._exc = data, exc

    def get_artifact(self, key, etag):
        if self._exc is not None:
            raise self._exc
        return FetchedArtifact(self._data, Sensitivity.SENSITIVE, "build")


def test_no_row_returns_none():
    got = asyncio.run(
        load_effective_config(_FakeConn(None), uuid4(), store_factory=lambda: _Store())
    )
    assert got is None


def test_present_config_parses():
    conn = _FakeConn({"object_key": "local/runs/x/effective_config"})
    got = asyncio.run(load_effective_config(conn, uuid4(), store_factory=lambda: _Store(_GOOD)))
    assert got is not None and got.is_enabled("KEXEC")


def test_store_error_fails_open_to_none():
    conn = _FakeConn({"object_key": "k"})
    exc = CategorizedError("gone", category=ErrorCategory.STALE_HANDLE)
    got = asyncio.run(
        load_effective_config(conn, uuid4(), store_factory=lambda: _Store(exc=exc))
    )
    assert got is None


def test_degenerate_config_fails_open_to_none():
    conn = _FakeConn({"object_key": "k"})
    got = asyncio.run(
        load_effective_config(conn, uuid4(), store_factory=lambda: _Store(b"# empty\n"))
    )
    assert got is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/kernel_config/test_fetch.py -q`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Write the implementation**

```python
# src/kdive/kernel_config/fetch.py
"""Fail-open reader for a Run's uploaded ``effective_config`` artifact (ADR-0318).

The config is SENSITIVE and Run-owned. This returns a parsed :class:`KernelConfig` only when a
real config is present; every failure mode (no row, store/DB error, degenerate parse) returns
``None`` so the caller arms as today rather than converting a benign advisory read into an
install/vmcore failure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.storage import FetchedArtifact
from kdive.domain.errors import CategorizedError
from kdive.kernel_config.parse import KernelConfig, parse_kernel_config
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)

# The Run-owned effective_config artifact (complete_build inserts owner_kind='runs').
_ROW_SQL = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'runs' AND owner_id = %s AND object_key LIKE %s LIMIT 1"
)
_KEY_SUFFIX = "%/effective_config"


class ConfigStore(Protocol):
    """The narrow object-store capability the reader needs (an ObjectStore satisfies it)."""

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...


async def load_effective_config(
    conn: AsyncConnection,
    run_id: UUID,
    *,
    store_factory: Callable[[], ConfigStore] = object_store_from_env,
) -> KernelConfig | None:
    """Return the Run's uploaded kernel config, or ``None`` when it cannot be read/trusted.

    ``None`` (arm-as-today) covers: no uploaded config, any store/DB error, and a degenerate
    (zero-enabled-symbol) upload. Never raises — the gate must not turn a config read into an
    action failure.
    """
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_ROW_SQL, (run_id, _KEY_SUFFIX))
            row = await cur.fetchone()
        if row is None:
            return None
        fetched = await asyncio.to_thread(store_factory().get_artifact, row["object_key"], None)
    except (CategorizedError, psycopg.Error, OSError) as exc:
        _log.warning("effective_config read failed for run %s; arming as today: %s", run_id, exc)
        return None
    config = parse_kernel_config(fetched.data)
    if config.is_degenerate:
        _log.warning("effective_config for run %s is degenerate; arming as today", run_id)
        return None
    return config
```

> Verify imports resolve: `kdive.domain.sensitivity.Sensitivity` (used only in the test's fake) and `kdive.artifacts.storage.FetchedArtifact`. If `Sensitivity` lives elsewhere, grep `class Sensitivity` and fix the test import.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/kernel_config/test_fetch.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/kernel_config/fetch.py tests/kernel_config/test_fetch.py
git commit -m "feat(kernel-config): fail-open effective_config reader (#1052)"
```

---

## Task 5: `artifacts.feature_config_requirements` MCP tool

**Files:**
- Create: `src/kdive/mcp/tools/catalog/artifacts/feature_requirements.py`
- Modify: `src/kdive/mcp/tools/catalog/artifacts/registrar.py`
- Test: `tests/mcp/catalog/test_feature_config_requirements_tool.py`

**Interfaces:**
- Consumes: `feature_manifest()` (Task 1); `ToolResponse` (`kdive.mcp.responses`); `_docmeta.read_only()`, `current_context()` (mirror `expected_uploads`).
- Produces: `FEATURE_CONFIG_REQUIREMENTS_TOOL = "artifacts.feature_config_requirements"`; `feature_config_requirements() -> ToolResponse` (static; `data={"features": feature_manifest()}`). Registered in `registrar.register` via `_register_artifacts_feature_config_requirements(app)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/catalog/test_feature_config_requirements_tool.py
from kdive.kernel_config.requirements import CRASH_CAPTURE
from kdive.mcp.tools.catalog.artifacts.feature_requirements import feature_config_requirements


def test_returns_advisory_manifest_of_every_feature():
    resp = feature_config_requirements()
    assert resp.status == "ok"
    features = resp.data["features"]
    ids = {f["feature"] for f in features}
    assert CRASH_CAPTURE in ids and "sysrq" in ids and "debuginfo" in ids
    crash = next(f for f in features if f["feature"] == CRASH_CAPTURE)
    assert crash["gated"] is True
    assert any("RANDOMIZE_BASE" in group for group in crash["requirements"])
    # advisory: no gate_required leak, no ADR strings anywhere
    assert "gate_required" not in crash
    assert "ADR" not in str(resp.data)
```

> **Confirm `ToolResponse` read attributes first** (`rg -n "class ToolResponse" -A40 src/kdive/mcp/responses.py`): this test reads `resp.status` and `resp.data`; Task 6 reads `resp.suggested_next_actions`. `ToolResponse.success(object_id, "ok", data=...)` and `ToolResponse.collection(..., suggested_next_actions=...)` are the constructors used by `expected_uploads.py`, so those attributes exist — but verify the exact names (some models expose `structured_content` instead of `data`) and adjust all three tasks' asserts consistently before writing them.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/catalog/test_feature_config_requirements_tool.py -q`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Write the tool + register it**

```python
# src/kdive/mcp/tools/catalog/artifacts/feature_requirements.py
"""``artifacts.feature_config_requirements`` — advisory feature -> CONFIG_* manifest (ADR-0318).

Static, read-only, auth-only (ADR-0117), the sibling of ``artifacts.expected_uploads``. It tells
an external kernel builder which ``CONFIG_*`` each debug/platform feature wants, so the agent can
build them in before uploading. Advisory only: kdive never validates the uploaded config, and an
agent may skip any feature.
"""

from __future__ import annotations

from kdive.kernel_config.requirements import feature_manifest
from kdive.mcp.responses import ToolResponse

FEATURE_CONFIG_REQUIREMENTS_TOOL = "artifacts.feature_config_requirements"

_OBJECT_ID = "feature-config-requirements"


def feature_config_requirements() -> ToolResponse:
    """Return the advisory feature -> required ``CONFIG_*`` manifest."""
    return ToolResponse.success(_OBJECT_ID, "ok", data={"features": feature_manifest()})
```

In `registrar.py`: add the import near the other tool imports, call the registrar in `register`, and add the `@app.tool` wrapper (mirror `_register_artifacts_expected_uploads`):

```python
# imports block
from kdive.mcp.tools.catalog.artifacts.feature_requirements import (
    feature_config_requirements as _feature_config_requirements,
)

# inside register():
    _register_artifacts_feature_config_requirements(app)

# new registrar function (place beside _register_artifacts_expected_uploads):
def _register_artifacts_feature_config_requirements(app: FastMCP) -> None:
    @app.tool(
        name="artifacts.feature_config_requirements",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def artifacts_feature_config_requirements() -> ToolResponse:
        """Advisory map of each debug/platform feature to the kernel ``CONFIG_*`` it needs.

        Read this before building a kernel to upload. Each ``data.features`` entry lists the
        feature, a ``summary``, ``gated`` (whether kdive refuses to arm it without the config),
        and ``requirements`` (OR-groups of ``CONFIG_*`` — any symbol in a group satisfies it).
        Advisory only: kdive never validates your config; skip any feature you do not need.
        Requires a token.
        """
        current_context()
        return _feature_config_requirements()
```

- [ ] **Step 4: Run test + full tool-index/registry tests to verify registration is clean**

Run: `uv run python -m pytest tests/mcp/catalog/test_feature_config_requirements_tool.py tests/mcp/test_tool_index.py -q`
Expected: PASS (the `artifacts` namespace already exists in `NAMESPACE_TOC`, so no completeness-guard change is needed).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/catalog/artifacts/feature_requirements.py src/kdive/mcp/tools/catalog/artifacts/registrar.py tests/mcp/catalog/test_feature_config_requirements_tool.py
git commit -m "feat(mcp): artifacts.feature_config_requirements advisory manifest tool (#1052)"
```

---

## Task 6: Cross-reference the tool from the build journey

**Files:**
- Modify: the `runs.create` and `artifacts.expected_uploads` `suggested_next_actions` (wrapper docstrings + next-action lists).
- Test: extend the existing next-actions assertions if present, else add a focused check.

**Interfaces:**
- Consumes: `FEATURE_CONFIG_REQUIREMENTS_TOOL` (Task 5).

- [ ] **Step 1: Locate the next-action lists**

Run: `rg -n "suggested_next_actions|EXPECTED_UPLOADS_TOOL|_NEXT_ACTIONS" src/kdive/mcp/tools/catalog/artifacts/expected_uploads.py src/kdive/mcp/tools/lifecycle/runs/create.py`

- [ ] **Step 2: Write/extend the failing test**

Add an assertion (in `tests/mcp/catalog/test_expected_uploads_tool.py`) that `expected_uploads()`'s response advertises `artifacts.feature_config_requirements` in its `suggested_next_actions` (or the item where next tools are listed). Mirror how that test currently asserts `_NEXT_ACTIONS`.

```python
def test_expected_uploads_points_at_feature_config_requirements():
    from kdive.mcp.tools.catalog.artifacts.expected_uploads import expected_uploads
    resp = expected_uploads()
    assert "artifacts.feature_config_requirements" in str(resp.suggested_next_actions or resp.data)
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/catalog/test_expected_uploads_tool.py -q -k feature_config`
Expected: FAIL.

- [ ] **Step 4: Add the tool to the next-actions**

In `expected_uploads.py`, add `FEATURE_CONFIG_REQUIREMENTS_TOOL` to `_NEXT_ACTIONS` (import it). In `runs/create.py`, add it to the external-lane `suggested_next_actions` beside `artifacts.expected_uploads`, and mention it in the wrapper docstring's build guidance (one sentence: "call `artifacts.feature_config_requirements` to learn which CONFIG_* each debug feature needs"). Avoid import cycles — import the string constant, not the tool function.

- [ ] **Step 5: Run + guardrails + commit**

```bash
uv run python -m pytest tests/mcp/catalog/test_expected_uploads_tool.py tests/mcp/lifecycle/test_runs_tools.py -q
just lint && just type
git add -A src/kdive/mcp tests/mcp
git commit -m "feat(mcp): surface feature_config_requirements in the build next-actions (#1052)"
```

---

## Task 7: Gate the install crashkernel seam

**Files:**
- Modify: `src/kdive/jobs/handlers/runs/install.py`
- Test: `tests/jobs/handlers/test_runs_install.py`

**Interfaces:**
- Consumes: `load_effective_config` (Task 4), `feature_requirement`/`CRASH_CAPTURE` (Task 1), `unmet_clauses`/`missing_symbols` (Task 3).
- Insert the gate **after** the existing `crashkernel_requires_kdump` backstop (`install.py:76-84`) and **before** `cmdline_for(...)`. Only runs when `crashkernel is not None`.

- [ ] **Step 1: Write the failing test**

Read `tests/jobs/handlers/test_runs_install.py` for the existing install-handler harness (how it builds a `conn`, a Run with `kernel_ref`, a `System`, a `resolver`, and an `InstallPayload` with `crashkernel`, and how it runs the coroutine — likely `asyncio.run`).

**Pin the config injection with `monkeypatch`, not artifact seeding.** Do **not** try to seed a real `SENSITIVE` `effective_config` artifact row + object bytes — patch the gate's read at its **import site in the handler module** so the seam test is independent of the object store:

```python
from unittest.mock import patch
from kdive.kernel_config.parse import KernelConfig

# a config with every crash gate_required symbol EXCEPT PROC_VMCORE
_MISSING = KernelConfig(frozenset({
    "KEXEC_CORE", "KEXEC", "CRASH_DUMP", "VMCORE_INFO", "FW_CFG_SYSFS", "RELOCATABLE",
}))


def test_install_refuses_crashkernel_when_config_lacks_crash_symbols(<harness>):
    async def _fake_load(conn, run_id, *, store_factory=None):
        return _MISSING
    with patch("kdive.jobs.handlers.runs.install.load_effective_config", _fake_load):
        with pytest.raises(CategorizedError) as ei:
            asyncio.run(install_handler(conn, job, resolver=resolver, ...))  # crashkernel="256M"
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ei.value.details["reason"] == "kernel_missing_crash_config"
    assert "PROC_VMCORE" in ei.value.details["missing"]
```

Add a second test asserting that with `_fake_load` returning `None` (no config) — **or** the default (unpatched) path when no crashkernel is requested — the install proceeds normally (no raise). Patching at the handler's import path (`kdive.jobs.handlers.runs.install.load_effective_config`) is required: patching `kdive.kernel_config.fetch.load_effective_config` would not affect the name already bound in the handler module.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_install.py -q -k crash`
Expected: FAIL (no gate yet — install proceeds).

- [ ] **Step 3: Add the gate**

After the `crashkernel_requires_kdump` block, before `kernel_ref = run.kernel_ref`:

```python
    if crashkernel is not None:
        config = await load_effective_config(conn, run_id)
        if config is not None:
            unmet = unmet_clauses(config, feature_requirement(CRASH_CAPTURE))
            if unmet:
                raise CategorizedError(
                    "uploaded kernel config lacks symbols required for kdump crash capture",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={
                        "reason": "kernel_missing_crash_config",
                        "missing": missing_symbols(unmet),
                        "remediation": (
                            "rebuild the kernel with the missing CONFIG_* (see "
                            "artifacts.feature_config_requirements) or install without a crashkernel"
                        ),
                    },
                )
```

Add imports:

```python
from kdive.kernel_config.fetch import load_effective_config
from kdive.kernel_config.requirements import CRASH_CAPTURE, feature_requirement
from kdive.kernel_config.support import missing_symbols, unmet_clauses
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_install.py -q`
Expected: PASS (both new tests + existing install tests).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/jobs/handlers/runs/install.py tests/jobs/handlers/test_runs_install.py
git commit -m "feat(runs): gate kdump crashkernel on uploaded config (#1052)"
```

---

## Task 8: Gate the kdump vmcore-fetch seam

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/vmcore_handlers.py`
- Test: `tests/mcp/lifecycle/test_vmcore_tools.py`

**Interfaces:**
- Consumes: `load_effective_config` (Task 4), `feature_requirement`/`CRASH_CAPTURE` (Task 1), `unmet_clauses`/`missing_symbols` (Task 3), the module's `_config_error(object_id, *, detail=..., data=...)` helper, `CaptureMethod` (already imported).
- Insert the gate in `_fetch_vmcore` **after** `capture_method = resolved` and **before** `_enqueue`, only when `capture_method is CaptureMethod.KDUMP`.

- [ ] **Step 1: Write the failing test**

In `tests/mcp/lifecycle/test_vmcore_tools.py`, following the existing `_fetch_vmcore` harness (seeds a CRASHED System bound to a Run, a runtime whose `supported_capture_methods` includes KDUMP; runs via `asyncio.run`). As in Task 7, **patch the gate read at the handler's import site** rather than seeding an artifact:

```python
from unittest.mock import patch
from kdive.kernel_config.parse import KernelConfig

_MISSING = KernelConfig(frozenset({  # every crash gate symbol except KEXEC_CORE
    "KEXEC", "CRASH_DUMP", "PROC_VMCORE", "VMCORE_INFO", "FW_CFG_SYSFS", "RELOCATABLE",
}))


def test_vmcore_kdump_refused_when_config_lacks_crash_symbols(<harness>):
    async def _fake_load(conn, run_id, *, store_factory=None):
        return _MISSING
    with patch("kdive.mcp.tools.lifecycle.vmcore_handlers.load_effective_config", _fake_load):
        resp = asyncio.run(_fetch_vmcore(pool, ctx, run_id=str(run_id), method="kdump", runtime=rt))
    assert resp.error_category == "configuration_error"
    assert resp.data["reason"] == "kernel_missing_crash_config"
    assert "KEXEC_CORE" in resp.data["missing"]
```

Add a **host_dump** test: same `_fake_load` (missing config) but `method="host_dump"` → the gate is skipped (`capture_method is CaptureMethod.KDUMP` is false), so the job enqueues normally (assert a success/`job` envelope, no `configuration_error`). Confirm `ToolResponse`'s failure attributes (`.error_category`, `.data`) against `kdive/mcp/responses.py` before writing the asserts.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_vmcore_tools.py -q -k crash_config`
Expected: FAIL (no gate — job enqueues).

- [ ] **Step 3: Add the gate**

```python
            resolved = _resolve_capture_method(run_id, method, system, runtime)
            if isinstance(resolved, ToolResponse):
                return resolved
            capture_method = resolved

            if capture_method is CaptureMethod.KDUMP:
                config = await load_effective_config(conn, uid)
                if config is not None:
                    unmet = unmet_clauses(config, feature_requirement(CRASH_CAPTURE))
                    if unmet:
                        return _config_error(
                            run_id,
                            detail=(
                                "uploaded kernel config lacks symbols required for a kdump vmcore"
                            ),
                            data={
                                "reason": "kernel_missing_crash_config",
                                "missing": missing_symbols(unmet),
                            },
                        )
```

Add imports (top of module):

```python
from kdive.kernel_config.fetch import load_effective_config
from kdive.kernel_config.requirements import CRASH_CAPTURE, feature_requirement
from kdive.kernel_config.support import missing_symbols, unmet_clauses
```

> Confirm `_config_error` accepts `detail=` and `data=` (it does elsewhere in this module, e.g. the `run_unbound` branch). Match its exact signature.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_vmcore_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/lifecycle/vmcore_handlers.py tests/mcp/lifecycle/test_vmcore_tools.py
git commit -m "feat(vmcore): gate kdump-method vmcore fetch on uploaded config (#1052)"
```

---

## Task 9: Enrich the sysrq no-output remediation

**Files:**
- Modify: `src/kdive/jobs/handlers/diagnostic_sysrq.py`
- Test: `tests/jobs/handlers/test_diagnostic_sysrq_handler.py`

**Interfaces:** none new. sysrq stays runtime-gated (Spec §4a); this only makes the existing `no_console_output` refusal name `MAGIC_SYSRQ`.

- [ ] **Step 1: Write/extend the failing test**

In `tests/jobs/handlers/test_diagnostic_sysrq_handler.py`, find the test that drives the `no_output` path (asserts `reason == "no_console_output"`). Add an assertion that the raised `CategorizedError`'s `details["remediation"]` contains `"MAGIC_SYSRQ"`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/jobs/handlers/test_diagnostic_sysrq_handler.py -q -k no_console_output`
Expected: FAIL (remediation does not yet mention MAGIC_SYSRQ).

- [ ] **Step 3: Edit the remediation string**

In `diagnostic_sysrq.py`, the `result.exit_reason == "no_output"` branch, extend the remediation:

```python
                "remediation": (
                    "build the guest kernel with CONFIG_MAGIC_SYSRQ=y (see "
                    "artifacts.feature_config_requirements) and a PS/2 keyboard driver "
                    "(i8042/atkbd), and enable kernel.sysrq for this command"
                ),
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/jobs/handlers/test_diagnostic_sysrq_handler.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/jobs/handlers/diagnostic_sysrq.py tests/jobs/handlers/test_diagnostic_sysrq_handler.py
git commit -m "feat(sysrq): name MAGIC_SYSRQ in the no-output remediation (#1052)"
```

---

## Task 10: Regenerate the agent-facing docs

**Files:**
- Modify: generated MCP tool reference (whatever `just docs` regenerates) + any doc-resource snapshots.

- [ ] **Step 1: Regenerate**

Run: `just docs && just resources-docs` (mutating). Then verify: `just docs-check && just resources-docs-check`.
Expected: the new `artifacts.feature_config_requirements` tool appears in the generated reference; checks pass.

- [ ] **Step 2: Full guardrail sweep**

Run: `just ci`
Expected: lint, type, lint-shell, lint-workflows, check-mermaid, test all green.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "docs(mcp): regenerate tool reference for feature_config_requirements (#1052)"
```

---

## Self-Review (checklist run against the spec)

**Spec coverage:**
- R1 advertise → Task 1 (registry) + Task 5 (tool) + Task 6 (discoverability). ✓
- R2 gate (Run-addressed) → Task 7 (install) + Task 8 (vmcore); sysrq runtime path → Task 9. ✓
- Registry advertised/gate_required split → Task 1. ✓
- Parser (=y/=m, not-set, degenerate) → Task 2. ✓
- Support/OR-groups/missing_symbols → Task 3. ✓
- Fail-open (no row / store error / degenerate) → Task 4. ✓
- Absent config arms-as-today → Tasks 4/7/8 (gate only when `config is not None`). ✓
- host_dump never gates → Task 8 (KDUMP-only guard). ✓
- No schema change → confirmed (no migration task). ✓
- Docs regen → Task 10. ✓
- gdbstub not gated → no task touches provisioning/xml (correct: excluded by design). ✓

**Type consistency:** `KernelConfig`, `feature_requirement`, `CRASH_CAPTURE`, `unmet_clauses`, `missing_symbols`, `load_effective_config`, `feature_manifest`, `FEATURE_CONFIG_REQUIREMENTS_TOOL` used identically across tasks. ✓

**Placeholder scan:** each code step carries full code; harness-dependent test bodies (Tasks 7/8) reference the existing seam-test files and describe exact seeded state + assertions rather than guessing private fixture names — read those files first. ✓

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-07-08-debug-feature-config-gate-1052.md`. Recommended execution: **subagent-driven** (fresh subagent per task, review between). Order 1→10; Tasks 1-4 are prerequisites for 5/7/8. Tasks 7 and 8 both import the Task 1/3/4 API but touch disjoint files, so they can run in parallel after Task 4.
