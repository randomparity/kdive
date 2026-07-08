# Build-Config Compose + Rootfs Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a server-build `config` compose as an ordered `list[ComponentRef]` that replaces the default kdump fragment, and guard the rootfs-mount symbols at build via the existing requirements seam, surfaced to the agent.

**Architecture:** `ServerBuildProfile.config` gains a list form; a single `config_refs()` normalizer replaces the three `or DEFAULT_CONFIG_REF` sites. Multiple fragments are collapsed by `effective_config_fragment()` (deterministic last-writer-wins per symbol, including disables) into one canonical fragment that flows unchanged through the existing single-blob merge seam — so the merged `.config` is our testable logic, not merge_config.sh's version-dependent within-file behavior, and the net-intent drop-check falls out for free. A new `PLATFORM_REQUIRED_CONFIG` requirements set is validated against the final `.config` in `_validate_final_config` and surfaced via `buildconfig.get`.

**Tech Stack:** Python 3.14, `uv`, pydantic v2, pytest, FastMCP. Spec: `docs/specs/2026-07-08-config-compose-list-1036.md`. ADR: `docs/adr/0316-compose-build-config-fragments.md`.

## Global Constraints

- Absolute imports only (no relative `..`); Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict, whole-tree (src + tests).
- Guardrails (run the justfile recipes, never reinvent): `just lint`, `just type`, `just test`, `just docs` (regenerates reference docs), `just docs-check` (fails on generated-doc drift), and `just ci` (full PR gate) before final push.
- The default (absent `config`) and existing single-ref `config` builds must be **byte-for-byte unchanged**: the raw resolved bytes and merge behavior must not change for the 0- or 1-ref case.
- No DB migration — the profile persists in the `runs.build_profile` JSONB; a list value round-trips.
- Prose/comment style: plain and factual; avoid "critical"/"robust"/"comprehensive"/"elegant"; no ADR-NNNN in agent-facing tool text (Field/wrapper docstrings).
- The wrapper docstring + `Field(description=...)` is the only agent-facing contract; update those, not only inner handlers, when a contract changes.
- Selection principle for the universal guard: only symbols every kdive System needs to *mount* its image, that `olddefconfig` will not auto-select. Capture-method symbols (`FW_CFG_SYSFS`, etc.) stay in `profile_requirements`, not this set. `CONFIG_CRASH_DUMP` + the debuginfo OR-group stay in the unchanged `REQUIRED_KERNEL_CONFIG` check.

---

## File Structure

- `src/kdive/profiles/build.py` — add `MAX_CONFIG_FRAGMENTS`, widen `config` to `ComponentRef | list[ComponentRef] | None`, add a length validator.
- `src/kdive/build_configs/platform_config.py` — **new**: `PLATFORM_REQUIRED_CONFIG` (`ConfigRequirements`), the relocated `REQUIRED_KERNEL_CONFIG` OR-groups, and `platform_required_payload()` builder. Single source for both the guard and the surface.
- `src/kdive/providers/shared/build_host/configuration/config.py` — add `config_refs(profile)`, `effective_config_fragment(fragments)`, `resolve_config_list_bytes(refs, ...)`.
- `src/kdive/providers/shared/build_host/orchestration.py` — `build_workspace` uses the list resolve path; `_validate_final_config` adds the `PLATFORM_REQUIRED_CONFIG` check; import `REQUIRED_KERNEL_CONFIG` from the new module.
- `src/kdive/mcp/tools/lifecycle/runs/server_build.py`, `.../composite.py` — iterate `config_refs(parsed)` for per-ref source/validator checks.
- `src/kdive/mcp/tools/catalog/build_configs.py` — add `data.platform_required_config` to `read_build_config`.
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — extend the `config` Field text.
- Tests: `tests/profiles/test_build.py`, `tests/providers/build_host/test_config_compose.py` (**new**), `tests/providers/build_host/test_orchestration.py`, `tests/build_configs/test_platform_config.py` (**new**), `tests/mcp/lifecycle/test_runs_tools.py` (server_build / `runs.build` harness), `tests/mcp/tools/lifecycle/runs/test_composite_tool.py` (`runs.build_install_boot`), `tests/mcp/catalog/test_build_configs_tool.py`.

Verify before coding (fast checks that keep import direction acyclic):
- `grep -n build_configs src/kdive/profiles/build.py` → **NO** (profiles must not import build_configs; the length validator uses only `MAX_CONFIG_FRAGMENTS` defined locally).
- `grep -n profiles src/kdive/build_configs/platform_config.py` → must stay NO (platform_config imports only `components.requirements`).

---

## Task 1: Widen `config` to a bounded list on `ServerBuildProfile`

**Files:**
- Modify: `src/kdive/profiles/build.py` (the `config` field at ~:115, add module constant + validator)
- Test: `tests/profiles/test_build.py`

**Interfaces:**
- Produces: `ServerBuildProfile.config: ComponentRef | list[ComponentRef] | None`; module constant `MAX_CONFIG_FRAGMENTS: int = 8`. An empty or over-cap list raises `ValidationError` → `BuildProfile.parse` maps it to `CategorizedError(CONFIGURATION_ERROR)`.

- [ ] **Step 1: Write failing tests**

Add to `tests/profiles/test_build.py`:

```python
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import MAX_CONFIG_FRAGMENTS, BuildProfile, ServerBuildProfile


def _server_doc(config):
    return {
        "schema_version": 1,
        "kernel_source_ref": "warm-tree-ref",
        "config": config,
    }


def _catalog(name):
    return {"kind": "catalog", "provider": "system", "name": name}


def test_config_accepts_a_list_of_refs():
    profile = BuildProfile.parse(_server_doc([_catalog("kdump"), _catalog("faultinject")]))
    assert isinstance(profile, ServerBuildProfile)
    assert isinstance(profile.config, list)
    assert [c.name for c in profile.config] == ["kdump", "faultinject"]


def test_config_still_accepts_a_single_ref():
    profile = BuildProfile.parse(_server_doc(_catalog("kdump")))
    assert isinstance(profile, ServerBuildProfile)
    assert not isinstance(profile.config, list)
    assert profile.config.name == "kdump"


def test_config_empty_list_is_configuration_error():
    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse(_server_doc([]))
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_config_over_cap_list_is_configuration_error():
    over = [_catalog(f"frag{i}") for i in range(MAX_CONFIG_FRAGMENTS + 1)]
    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse(_server_doc(over))
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/profiles/test_build.py -k "config_accepts_a_list or single_ref or empty_list or over_cap" -q`
Expected: FAIL (`ImportError: MAX_CONFIG_FRAGMENTS` / list not accepted).

- [ ] **Step 3: Implement**

In `src/kdive/profiles/build.py`, add near the top-level constants:

```python
MAX_CONFIG_FRAGMENTS = 8
```

Change the `config` field on `ServerBuildProfile` from `config: ComponentRef | None = None` to:

```python
    config: ComponentRef | list[ComponentRef] | None = None
```

Add a validator to `ServerBuildProfile` (place beside `_reject_uri_bare_source`):

```python
    @field_validator("config", mode="after")
    @classmethod
    def _bounded_config_list(
        cls, value: ComponentRef | list[ComponentRef] | None
    ) -> ComponentRef | list[ComponentRef] | None:
        """A list ``config`` composes fragments in order; bound it to 1..MAX_CONFIG_FRAGMENTS.

        An empty list is neither "absent" (which resolves the default) nor a valid compose;
        an over-cap list would open an unbounded number of per-ref catalog fetches.
        """
        if isinstance(value, list) and not (1 <= len(value) <= MAX_CONFIG_FRAGMENTS):
            raise ValueError(
                f"config list must have 1..{MAX_CONFIG_FRAGMENTS} entries, got {len(value)}"
            )
        return value
```

Confirm `field_validator` is already imported (it is — used by `_reject_uri_bare_source`).

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/profiles/test_build.py -q`
Expected: PASS (all, including pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/profiles/build.py tests/profiles/test_build.py
git commit -m "feat(1036): accept a bounded list form for build-profile config"
```

---

## Task 2: Compose helpers — `config_refs`, `effective_config_fragment`, `resolve_config_list_bytes`

**Files:**
- Modify: `src/kdive/providers/shared/build_host/configuration/config.py`
- Test: `tests/providers/build_host/test_config_compose.py` (**new**)

**Interfaces:**
- Consumes: `ServerBuildProfile` (Task 1); `DEFAULT_CONFIG_REF`, `CatalogConfigFetch` (`kdive.build_configs.defaults`); `resolve_config_bytes` (same module).
- Produces:
  - `config_refs(profile: ServerBuildProfile) -> list[ComponentRef]` — `[DEFAULT_CONFIG_REF]` when absent, the list as-is, or `[ref]` for a single ref.
  - `effective_config_fragment(fragments: list[bytes]) -> bytes` — canonical last-writer-wins fragment; only used when composing (>1).
  - `resolve_config_list_bytes(refs, *, allowed_component_roots, catalog_fetch) -> bytes` — resolves each ref, returns the single raw fragment for 1 ref (byte-for-byte) or `effective_config_fragment` of all for >1.

- [ ] **Step 1: Write failing tests**

Create `tests/providers/build_host/test_config_compose.py`:

```python
from __future__ import annotations

from pathlib import Path

from kdive.build_configs.defaults import DEFAULT_CONFIG_REF
from kdive.components.references import CatalogComponentRef
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.shared.build_host.configuration.config import (
    config_refs,
    effective_config_fragment,
    resolve_config_list_bytes,
)


def _profile(config) -> ServerBuildProfile:
    profile = BuildProfile.parse(
        {"schema_version": 1, "kernel_source_ref": "warm-ref", "config": config}
    )
    assert isinstance(profile, ServerBuildProfile)
    return profile


def _cat(name: str) -> dict:
    return {"kind": "catalog", "provider": "system", "name": name}


def test_config_refs_absent_yields_default():
    assert config_refs(_profile(None)) == [DEFAULT_CONFIG_REF]


def test_config_refs_single_wraps_in_list():
    refs = config_refs(_profile(_cat("kdump")))
    assert refs == [CatalogComponentRef(kind="catalog", provider="system", name="kdump")]


def test_config_refs_list_preserves_order():
    refs = config_refs(_profile([_cat("kdump"), _cat("faultinject")]))
    assert [r.name for r in refs] == ["kdump", "faultinject"]


def test_effective_fragment_later_value_wins():
    frags = [b"CONFIG_FOO=y\nCONFIG_BAR=y\n", b"CONFIG_FOO=m\n"]
    out = effective_config_fragment(frags).decode()
    assert "CONFIG_FOO=m" in out
    assert "CONFIG_FOO=y" not in out
    assert "CONFIG_BAR=y" in out


def test_effective_fragment_later_disable_wins():
    frags = [b"CONFIG_FOO=y\n", b"# CONFIG_FOO is not set\n"]
    out = effective_config_fragment(frags).decode()
    assert "# CONFIG_FOO is not set" in out
    assert "CONFIG_FOO=y" not in out


def test_effective_fragment_drops_comments_and_blanks():
    out = effective_config_fragment([b"# a comment\n\nCONFIG_FOO=y\n"]).decode()
    assert out.strip() == "CONFIG_FOO=y"


def test_resolve_single_ref_is_raw_bytes_unchanged():
    raw = b"# comment kept verbatim\nCONFIG_FOO=y\n"
    got = resolve_config_list_bytes(
        [CatalogComponentRef(kind="catalog", provider="system", name="kdump")],
        allowed_component_roots=[Path("/nonexistent")],
        catalog_fetch=lambda _n: raw,
    )
    assert got == raw  # single-ref path must not normalize


def test_resolve_multi_ref_returns_effective_fragment():
    fetches = {"a": b"CONFIG_FOO=y\n", "b": b"CONFIG_FOO=m\n"}
    got = resolve_config_list_bytes(
        [
            CatalogComponentRef(kind="catalog", provider="system", name="a"),
            CatalogComponentRef(kind="catalog", provider="system", name="b"),
        ],
        allowed_component_roots=[Path("/nonexistent")],
        catalog_fetch=lambda n: fetches[n],
    ).decode()
    assert "CONFIG_FOO=m" in got and "CONFIG_FOO=y" not in got
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/build_host/test_config_compose.py -q`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement**

In `src/kdive/providers/shared/build_host/configuration/config.py` add imports:

```python
from kdive.build_configs.defaults import DEFAULT_CONFIG_REF
from kdive.profiles.build import ServerBuildProfile
```

(`CatalogConfigFetch` is already imported.) Then add:

```python
def config_refs(profile: ServerBuildProfile) -> list[ComponentRef]:
    """The ordered config refs a build resolves: the default when absent, else the profile's.

    A single ref wraps to a one-element list; a list is returned as-is. This is the single
    source that replaces the scattered ``profile.config or DEFAULT_CONFIG_REF`` idiom, so the
    resolve site and the run-creation validation sites cannot diverge.
    """
    if profile.config is None:
        return [DEFAULT_CONFIG_REF]
    if isinstance(profile.config, list):
        return list(profile.config)
    return [profile.config]


def effective_config_fragment(fragments: list[bytes]) -> bytes:
    """Collapse ordered fragments into one canonical fragment, last-writer-wins per symbol.

    Each ``CONFIG_x=<val>`` sets the symbol; each ``# CONFIG_x is not set`` unsets it; a later
    line for the same symbol overrides an earlier one across every fragment. Emitted in
    first-seen order so a composed set merges deterministically (independent of merge_config.sh's
    within-file duplicate handling). Comments and blank lines are inert and dropped.
    """
    values: dict[str, str | None] = {}
    for raw in fragments:
        for line in raw.decode().splitlines():
            stripped = line.strip()
            if stripped.startswith("# CONFIG_") and stripped.endswith(" is not set"):
                values[stripped[len("# ") : -len(" is not set")]] = None
            elif stripped.startswith("CONFIG_") and "=" in stripped:
                symbol, _, value = stripped.partition("=")
                values[symbol] = value
    lines = [
        f"{symbol}={value}" if value is not None else f"# {symbol} is not set"
        for symbol, value in values.items()
    ]
    return ("\n".join(lines) + "\n").encode()


def resolve_config_list_bytes(
    refs: list[ComponentRef],
    *,
    allowed_component_roots: list[Path],
    catalog_fetch: CatalogConfigFetch,
) -> bytes:
    """Resolve ordered config refs to fragment bytes for the merge step.

    A single ref returns its raw resolved bytes unchanged (the default/single-config path stays
    byte-for-byte). Multiple refs are resolved in order and collapsed by
    :func:`effective_config_fragment` so the merged ``.config`` reflects last-writer-wins.
    """
    resolved = [
        resolve_config_bytes(
            ref, allowed_component_roots=allowed_component_roots, catalog_fetch=catalog_fetch
        )
        for ref in refs
    ]
    if len(resolved) == 1:
        return resolved[0]
    return effective_config_fragment(resolved)
```

If `grep -n profiles src/kdive/build_configs/defaults.py` shows defaults imports profiles (it must not — verified NO in preflight), and importing `ServerBuildProfile` into `config.py` raises a circular import at runtime, move `config_refs` to consume the profile via a `TYPE_CHECKING` import plus a runtime `isinstance` on `list` only (it does not need the class at runtime except for the type hint). The functions here only touch `.config`, so a `TYPE_CHECKING`-guarded import is sufficient if a cycle appears.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/build_host/test_config_compose.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/build_host/configuration/config.py tests/providers/build_host/test_config_compose.py
git commit -m "feat(1036): add config_refs + effective-fragment compose helpers"
```

---

## Task 3: Resolve the list at the build execution site

**Files:**
- Modify: `src/kdive/providers/shared/build_host/orchestration.py:106-124` (`build_workspace`)
- Test: `tests/providers/build_host/test_orchestration.py`

**Interfaces:**
- Consumes: `config_refs`, `resolve_config_list_bytes` (Task 2).
- Produces: `build_workspace` resolves `config_refs(profile)` via `resolve_config_list_bytes`; the resulting `fragment_bytes`/`fragment_text` feed the existing `checkout` and `_validate_final_config` unchanged.

- [ ] **Step 1: Write failing tests**

Add to `tests/providers/build_host/test_orchestration.py` (reuse the file's `_validating_orchestrator`; extend it to key the fetch by name for compose):

```python
def test_build_workspace_composes_two_catalog_fragments(tmp_path: Path) -> None:
    # A two-fragment compose resolves the union; the later fragment's value wins.
    fetches = {"kdump": b"CONFIG_FOO=y\n", "faultinject": b"CONFIG_FOO=m\nCONFIG_FAULT=y\n"}
    orchestrator = BuildHostOrchestrator.create(
        workspace_root=tmp_path,
        catalog_fetch=lambda name: fetches[name],
        checkout=lambda _r, _p, _w, fragment: _captured.append(fragment),
        run_olddefconfig=lambda _w: CapturedStep(0, ""),
        read_config=lambda _w: "CONFIG_FOO=m\nCONFIG_FAULT=y\n" + _GOOD_TAIL,
        run_make=lambda _w: CapturedStep(0, ""),
    )
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": "warm-ref",
            "config": [
                {"kind": "catalog", "provider": "system", "name": "kdump"},
                {"kind": "catalog", "provider": "system", "name": "faultinject"},
            ],
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    orchestrator.build_workspace(_RUN, profile)
    merged = _captured[-1].decode()
    assert "CONFIG_FOO=m" in merged and "CONFIG_FOO=y" not in merged


def test_build_workspace_compose_later_disable_is_not_a_dropped_symbol(tmp_path: Path) -> None:
    # Acceptance: a later fragment disabling an earlier =y symbol builds successfully — the
    # net-intent effective fragment emits it as unset, so the drop-check does not flag it.
    fetches = {
        "kdump": b"CONFIG_FOO=y\nCONFIG_SQUASHFS=y\n",
        "faultinject": b"# CONFIG_FOO is not set\n",
    }
    orchestrator = BuildHostOrchestrator.create(
        workspace_root=tmp_path,
        catalog_fetch=lambda name: fetches[name],
        checkout=lambda _r, _p, _w, _f: None,
        run_olddefconfig=lambda _w: CapturedStep(0, ""),
        # FOO is correctly off in the final config; the build must NOT be rejected.
        read_config=lambda _w: "# CONFIG_FOO is not set\n" + _GOOD_TAIL,
        run_make=lambda _w: CapturedStep(0, ""),
    )
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": "warm-ref",
            "config": [
                {"kind": "catalog", "provider": "system", "name": "kdump"},
                {"kind": "catalog", "provider": "system", "name": "faultinject"},
            ],
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    # Does not raise (no spurious "dropped by olddefconfig" for the intentional disable).
    orchestrator.build_workspace(_RUN, profile)


def test_build_workspace_single_ref_passes_raw_bytes(tmp_path: Path) -> None:
    # The single-config path must hand the checkout seam the raw fetched bytes unchanged.
    raw = b"# verbatim comment\nCONFIG_SQUASHFS=y\n" + _GOOD_TAIL.encode()
    seen: list[bytes] = []
    orchestrator = BuildHostOrchestrator.create(
        workspace_root=tmp_path,
        catalog_fetch=lambda _n: raw,
        checkout=lambda _r, _p, _w, fragment: seen.append(fragment),
        run_olddefconfig=lambda _w: CapturedStep(0, ""),
        read_config=lambda _w: raw.decode(),
        run_make=lambda _w: CapturedStep(0, ""),
    )
    orchestrator.build_workspace(_RUN, _server_profile())
    assert seen[-1] == raw
```

At the top of the test module add the shared list and a passing-config tail used by these and Task 4 (place after `_RUN`):

```python
_captured: list[bytes] = []
# A final .config tail that satisfies every always-on guard: the five mount symbols,
# CONFIG_CRASH_DUMP, and one debuginfo option.
_GOOD_TAIL = (
    "CONFIG_SQUASHFS=y\nCONFIG_SQUASHFS_ZSTD=y\nCONFIG_OVERLAY_FS=y\n"
    "CONFIG_BLK_DEV_LOOP=y\nCONFIG_XFS_FS=y\nCONFIG_CRASH_DUMP=y\n"
    "CONFIG_DEBUG_INFO_DWARF5=y\n"
)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/build_host/test_orchestration.py -k "composes_two or single_ref_passes_raw or later_disable" -q`
Expected: FAIL (compose not wired; `build_workspace` resolves a single ref only).

- [ ] **Step 3: Implement**

In `orchestration.py` `build_workspace`, replace:

```python
        config_ref = profile.config or DEFAULT_CONFIG_REF
        fragment_bytes = resolve_config_bytes(
            config_ref,
            allowed_component_roots=self.allowed_component_roots,
            catalog_fetch=self.catalog_fetch,
        )
```

with:

```python
        fragment_bytes = resolve_config_list_bytes(
            config_refs(profile),
            allowed_component_roots=self.allowed_component_roots,
            catalog_fetch=self.catalog_fetch,
        )
```

Fix the imports by **editing the existing blocks** (do not add a new import line — re-importing `validate_config_ref` would be an F811/F401 the lint gate rejects):
- In the existing `from ...configuration.config import (...)` block (currently names `DEFAULT_BUILD_COMPONENT_ROOT, load_profile_config_requirements, missing_config_groups, resolve_config_bytes, validate_config_ref`), **add** `config_refs` and `resolve_config_list_bytes` and **remove** `resolve_config_bytes` (now unused). Keep `validate_config_ref`, `missing_config_groups`, `load_profile_config_requirements`, `DEFAULT_BUILD_COMPONENT_ROOT`.
- In `from kdive.build_configs.defaults import DEFAULT_CONFIG_REF, CatalogConfigFetch`, **remove** `DEFAULT_CONFIG_REF` (now unused), keeping `CatalogConfigFetch`.

Confirm with `grep -n "resolve_config_bytes\|DEFAULT_CONFIG_REF" src/kdive/providers/shared/build_host/orchestration.py` (expect no remaining references after the edit). `validate_config_requirements` is already imported at the top of the file — no import change needed for Task 4.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/build_host/test_orchestration.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/build_host/orchestration.py tests/providers/build_host/test_orchestration.py
git commit -m "feat(1036): resolve composed config list at the build execution site"
```

---

## Task 4: Platform rootfs-mount guard + surfaced payload constant

**Files:**
- Create: `src/kdive/build_configs/platform_config.py`
- Modify: `src/kdive/providers/shared/build_host/orchestration.py` (`_validate_final_config`, import `REQUIRED_KERNEL_CONFIG` from the new module; add the platform check)
- Test: `tests/build_configs/test_platform_config.py` (**new**), `tests/providers/build_host/test_orchestration.py`

**Interfaces:**
- Produces:
  - `PLATFORM_REQUIRED_CONFIG: ConfigRequirements` (the five mount symbols, `=y`).
  - `REQUIRED_KERNEL_CONFIG: tuple[tuple[str, ...], ...]` (relocated, unchanged: crash-dump + debuginfo OR-group).
  - `platform_required_payload() -> dict` — `{"all_of": {...}, "any_of": [[...], ...]}` derived from the two constants.
- Consumes (guard): `validate_config_requirements` and `missing_config_groups` (already used in `_validate_final_config`).

- [ ] **Step 1: Write failing tests (constant + payload)**

Create `tests/build_configs/test_platform_config.py`:

```python
from __future__ import annotations

from kdive.build_configs.platform_config import (
    PLATFORM_REQUIRED_CONFIG,
    REQUIRED_KERNEL_CONFIG,
    platform_required_payload,
)
from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH


def _kdump_declarations() -> dict[str, str]:
    text = KDUMP_FRAGMENT_PATH.read_text()
    declared = {}
    for line in text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, _, value = line.partition("=")
            declared[key] = value
    return declared


def test_payload_is_derived_from_the_enforced_constants():
    payload = platform_required_payload()
    assert payload["all_of"] == dict(PLATFORM_REQUIRED_CONFIG.required)
    assert payload["any_of"] == [list(group) for group in REQUIRED_KERNEL_CONFIG]


def test_drift_guard_all_of_symbols_declared_in_seed():
    declared = _kdump_declarations()
    for symbol, value in PLATFORM_REQUIRED_CONFIG.required.items():
        assert declared.get(symbol) == value, symbol


def test_drift_guard_each_or_group_has_a_seeded_member():
    declared = _kdump_declarations()
    for group in REQUIRED_KERNEL_CONFIG:
        assert any(declared.get(sym) == "y" for sym in group), group
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/build_configs/test_platform_config.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the module**

Create `src/kdive/build_configs/platform_config.py`:

```python
"""Always-on platform kernel-config requirements (ADR-0316).

One source of truth for the symbols every kdive server build must carry, shared by the build
guard (``_validate_final_config``) and the agent-facing surface (``buildconfig.get``). The
universal set is scoped to the rootfs-*mount* symbols every System needs regardless of capture
method and that ``olddefconfig`` will not auto-select; capture-method symbols live in
per-method ``profile_requirements``.
"""

from __future__ import annotations

from kdive.components.requirements import ConfigRequirements

# Exact `=y` requirements: rootfs/boot-mount symbols. Not auto-selected by olddefconfig.
PLATFORM_REQUIRED_CONFIG = ConfigRequirements(
    required={
        "CONFIG_SQUASHFS": "y",
        "CONFIG_SQUASHFS_ZSTD": "y",
        "CONFIG_OVERLAY_FS": "y",
        "CONFIG_BLK_DEV_LOOP": "y",
        "CONFIG_XFS_FS": "y",
    }
)

# Pre-existing always-on check, relocated unchanged: crash-dump + the debuginfo OR-group.
REQUIRED_KERNEL_CONFIG: tuple[tuple[str, ...], ...] = (
    ("CONFIG_CRASH_DUMP",),
    ("CONFIG_DEBUG_INFO_DWARF4", "CONFIG_DEBUG_INFO_DWARF5", "CONFIG_DEBUG_INFO_BTF"),
)

PLATFORM_CONFIG_SYMBOL_MISSING = "platform_config_symbol_missing"


def platform_required_payload() -> dict[str, object]:
    """The surfaced platform requirement, derived from the constants the build guard enforces."""
    return {
        "all_of": dict(PLATFORM_REQUIRED_CONFIG.required),
        "any_of": [list(group) for group in REQUIRED_KERNEL_CONFIG],
    }


__all__ = [
    "PLATFORM_CONFIG_SYMBOL_MISSING",
    "PLATFORM_REQUIRED_CONFIG",
    "REQUIRED_KERNEL_CONFIG",
    "platform_required_payload",
]
```

- [ ] **Step 4: Run to verify pass (module)**

Run: `uv run python -m pytest tests/build_configs/test_platform_config.py -q`
Expected: PASS.

- [ ] **Step 5: Write failing guard tests**

Add to `tests/providers/build_host/test_orchestration.py`:

```python
def test_build_workspace_rejects_missing_mount_symbol(tmp_path: Path) -> None:
    # A final .config with crash-dump/debuginfo but missing a mount symbol fails with the
    # platform reason.
    final = "CONFIG_CRASH_DUMP=y\nCONFIG_DEBUG_INFO_DWARF5=y\nCONFIG_SQUASHFS=y\n"  # no OVERLAY_FS/LOOP/XFS/ZSTD
    orchestrator = _validating_orchestrator(
        tmp_path, fragment=b"CONFIG_SQUASHFS=y\n", final_config=final
    )
    with pytest.raises(CategorizedError) as caught:
        orchestrator.build_workspace(_RUN, _server_profile())
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details["reason"] == "platform_config_symbol_missing"
    assert "CONFIG_OVERLAY_FS" in caught.value.details["missing"]


def test_build_workspace_missing_crash_dump_keeps_existing_shape(tmp_path: Path) -> None:
    # CONFIG_CRASH_DUMP stays in REQUIRED_KERNEL_CONFIG: its failure keeps missing_any_of.
    final = _GOOD_TAIL.replace("CONFIG_CRASH_DUMP=y\n", "# CONFIG_CRASH_DUMP is not set\n")
    orchestrator = _validating_orchestrator(
        tmp_path, fragment=b"CONFIG_SQUASHFS=y\n", final_config=final
    )
    with pytest.raises(CategorizedError) as caught:
        orchestrator.build_workspace(_RUN, _server_profile())
    assert caught.value.details["missing_any_of"] == [["CONFIG_CRASH_DUMP"]]


def test_build_workspace_accepts_a_good_final_config(tmp_path: Path) -> None:
    orchestrator = _validating_orchestrator(
        tmp_path, fragment=b"CONFIG_SQUASHFS=y\n", final_config=_GOOD_TAIL
    )
    # Does not raise.
    orchestrator.build_workspace(_RUN, _server_profile())
```

Note: `_validating_orchestrator`'s `fragment` must be a subset of the mount symbols so the
drop-check does not fire first; use a minimal `CONFIG_SQUASHFS=y\n` present in `_GOOD_TAIL`.

- [ ] **Step 6: Run to verify failure**

Run: `uv run python -m pytest tests/providers/build_host/test_orchestration.py -k "missing_mount_symbol or missing_crash_dump or accepts_a_good" -q`
Expected: FAIL (mount guard not enforced; the "missing_mount_symbol" case currently passes validation).

- [ ] **Step 7: Implement the guard**

In `orchestration.py`:
- Replace the module-local `REQUIRED_KERNEL_CONFIG` definition (~:29-32) with an import:

```python
from kdive.build_configs.platform_config import (
    PLATFORM_CONFIG_SYMBOL_MISSING,
    PLATFORM_REQUIRED_CONFIG,
    REQUIRED_KERNEL_CONFIG,
)
```

- Ensure `validate_config_requirements` is imported (from `kdive.components.requirements`).
- In `_validate_final_config`, add the platform check **before** the profile-requirements block and after the existing group check:

```python
    try:
        validate_config_requirements(config_text, PLATFORM_REQUIRED_CONFIG)
    except CategorizedError as exc:
        raise CategorizedError(
            "kernel .config omits a platform-required rootfs symbol",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "reason": PLATFORM_CONFIG_SYMBOL_MISSING,
                "missing": exc.details.get("missing_or_different", []),
            },
        ) from exc
```

- [ ] **Step 8: Run to verify pass**

Run: `uv run python -m pytest tests/providers/build_host/test_orchestration.py tests/build_configs/test_platform_config.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/kdive/build_configs/platform_config.py src/kdive/providers/shared/build_host/orchestration.py tests/build_configs/test_platform_config.py tests/providers/build_host/test_orchestration.py
git commit -m "feat(1036): guard rootfs-mount symbols via the requirements seam"
```

---

## Task 5: Per-ref validation at run creation (server_build + composite)

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/server_build.py:83-96`
- Modify: `src/kdive/mcp/tools/lifecycle/runs/composite.py:118-130`
- Test (server_build / `runs.build`): `tests/mcp/lifecycle/test_runs_tools.py` — mirror the existing config-validator harness (`_reject_config` / `_CATALOG_COMPONENT_SOURCES` / `BuildRunHandlers(...config_validator=...)` at ~L2752-2962).
- Test (composite / `runs.build_install_boot`): `tests/mcp/tools/lifecycle/runs/test_composite_tool.py` — mirror its existing single-ref config-source rejection test.

**Interfaces:**
- Consumes: `config_refs` (Task 2).
- Produces: both handlers run `reject_unsupported_component_source` and (when present) `config_validator` for **every** ref in `config_refs(parsed)`, failing on the first bad ref. Behavior for a single/absent config is identical to today.

- [ ] **Step 1: Write failing tests (both handlers)**

The two handlers' tests live in two different trees; add one test to **each**. Reuse each module's existing Run-creation, pool, and `ComponentSourceCapabilities`/`config_validator` fixtures — do not invent new ones. The shared assertion: a compose list whose *second* ref is rejected (by an unsupported source kind, or by the module's injected `config_validator`) fails the build call with `CONFIGURATION_ERROR`, proving every ref is checked, not just the first.

In `tests/mcp/lifecycle/test_runs_tools.py` (mirror the `_reject_config` test at ~L2905 that injects `config_validator=_reject_config` and asserts a single catalog ref is rejected — extend it so the profile's `config` is a two-element list and the rejection still fires):

```python
async def test_build_rejects_a_bad_ref_within_a_compose_list(...):
    # Same harness as the single-ref _reject_config test, but config is a list; the injected
    # config_validator rejects, proving each ref in the list is validated.
    # config: [{catalog kdump}, {catalog kdump}]  (validator rejects on call)
    ...
    assert response.error_category is ErrorCategory.CONFIGURATION_ERROR
```

In `tests/mcp/tools/lifecycle/runs/test_composite_tool.py`, add the analogous list-config rejection test mirroring that module's existing single-ref source-rejection test.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py tests/mcp/tools/lifecycle/runs/test_composite_tool.py -k "compose_list or within_a_compose_list" -q`
Expected: FAIL (only one ref is checked today; a list is not iterated).

- [ ] **Step 3: Implement**

In `server_build.py`, replace:

```python
                config_ref = parsed.config or DEFAULT_CONFIG_REF
                try:
                    reject_unsupported_component_source(
                        self.component_sources,
                        component_kind=CONFIG_COMPONENT,
                        ref=config_ref,
                    )
                except CategorizedError as exc:
                    return ToolResponse.failure_from_error(run_id, exc)
                if self.config_validator is not None:
                    try:
                        self.config_validator(config_ref)
                    except CategorizedError as exc:
                        return ToolResponse.failure_from_error(run_id, exc)
```

with:

```python
                try:
                    for config_ref in config_refs(parsed):
                        reject_unsupported_component_source(
                            self.component_sources,
                            component_kind=CONFIG_COMPONENT,
                            ref=config_ref,
                        )
                        if self.config_validator is not None:
                            self.config_validator(config_ref)
                except CategorizedError as exc:
                    return ToolResponse.failure_from_error(run_id, exc)
```

Add `from kdive.providers.shared.build_host.configuration.config import config_refs` and drop the now-unused `DEFAULT_CONFIG_REF` import if unused elsewhere in the file (`grep -n DEFAULT_CONFIG_REF <file>`). Apply the identical change to `composite.py` (same block at :118-130).

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py tests/mcp/tools/lifecycle/runs/test_composite_tool.py -q`
Expected: PASS (both new tests + existing single-ref tests in both modules).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/runs/server_build.py src/kdive/mcp/tools/lifecycle/runs/composite.py tests/mcp/lifecycle/test_runs_tools.py tests/mcp/tools/lifecycle/runs/test_composite_tool.py
git commit -m "feat(1036): validate every composed config ref at run creation"
```

---

## Task 6: Surface `platform_required_config` on `buildconfig.get`

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/build_configs.py:119-129` (`read_build_config`)
- Test: `tests/mcp/catalog/test_build_configs_tool.py`

**Interfaces:**
- Consumes: `platform_required_payload` (Task 4).
- Produces: `buildconfig.get` response `data.platform_required_config == platform_required_payload()`.

- [ ] **Step 1: Write failing test**

Add to `tests/mcp/catalog/test_build_configs_tool.py` (reuse the module's `read_build_config` harness):

```python
from kdive.build_configs.platform_config import platform_required_payload


async def test_get_surfaces_platform_required_config(...):
    response = await read_build_config(conn, store, name="kdump")
    assert response.data["platform_required_config"] == platform_required_payload()
    # surfaced == enforced: the all_of set matches the guard's exact requirements.
    assert response.data["platform_required_config"]["all_of"]["CONFIG_SQUASHFS"] == "y"
```

(Match the existing get-tool test's connection/store fixtures.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/catalog/test_build_configs_tool.py -k "platform_required_config" -q`
Expected: FAIL (`KeyError`).

- [ ] **Step 3: Implement**

In `build_configs.py`, add the import `from kdive.build_configs.platform_config import platform_required_payload` and extend the `read_build_config` success `data`:

```python
        data={
            "content": data.decode(),
            "sha256": entry.sha256,
            "source": entry.source,
            "merge_recipe": _MERGE_RECIPE,
            "config_ref": catalog_config_ref(entry.name).model_dump(),
            "platform_required_config": platform_required_payload(),
        },
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/catalog/test_build_configs_tool.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/build_configs.py tests/mcp/catalog/test_build_configs_tool.py
git commit -m "feat(1036): surface platform_required_config on buildconfig.get"
```

---

## Task 7: Agent-facing `config` Field text + generated-doc regen

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py` (the `config` `Field(description=...)` for the server-build create tools; located near :86-88 where it already mentions `buildconfig.get`)
- Regenerate: `docs/guide/reference/runs.md` (and any other generated reference) via `just docs`
- Test: covered by the generated-doc guard (`just docs-check`) and the doc snapshot/completeness tests

**Interfaces:** none (documentation contract only).

- [ ] **Step 1: Update the Field text**

Extend the `config` Field description to state: a list composes fragments in order and **fully replaces** the default kdump fragment (list `{catalog:kdump}` explicitly to keep it); the build must satisfy the platform-required symbols shown in `buildconfig.get` `data.platform_required_config` or it fails with a `configuration_error`. Keep it plain, no ADR references, ≤ the surrounding style. Example addition:

```
"A single config replaces the seeded kdump fragment; a list of configs composes them in "
"order (later wins) and also replaces the default - list the kdump config explicitly to keep "
"it. Every build must still satisfy buildconfig.get data.platform_required_config (rootfs and "
"crash-dump symbols) or it fails with a configuration_error."
```

- [ ] **Step 2: Regenerate docs**

Run: `just docs`
Then: `git status --short docs/` — expect `docs/guide/reference/runs.md` (and possibly a buildconfig reference) updated.

- [ ] **Step 3: Verify the doc guard passes**

Run: `just docs-check`
Expected: PASS (no drift). Also run any doc snapshot tests: `uv run python -m pytest tests/mcp -k "doc or guide or snapshot" -q` (skip if none match).

- [ ] **Step 4: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/runs/registrar.py docs/guide/reference/
git commit -m "docs(1036): document config compose + platform-required in the config Field"
```

---

## Task 8: Full guardrail sweep

**Files:** none (verification only).

- [ ] **Step 1: Regression — single/absent config unchanged**

Run: `uv run python -m pytest tests/providers/build_host tests/profiles tests/build_configs tests/mcp/catalog tests/mcp/lifecycle/test_runs_tools.py tests/mcp/tools/lifecycle/runs/test_composite_tool.py -q`
Expected: PASS, including the pre-existing single-ref and default-config tests (byte-for-byte guarantee).

- [ ] **Step 2: Lint + types**

Run: `just lint && just type`
Expected: clean (fix any Ruff `E,F,I,UP,B,SIM` or `ty` findings; do not narrow `ty` scope).

- [ ] **Step 3: Full PR gate**

Run: `just ci`
Expected: green (lint, type, lint-shell, lint-workflows, check-mermaid, test).

- [ ] **Step 4: Commit any fixups**

```bash
git add -A
git commit -m "chore(1036): guardrail fixups"   # only if step 2/3 required changes
```

---

## Rollback / cleanup

- Each task is an isolated commit; revert a task's commit to back it out. The feature is additive — reverting all commits restores byte-for-byte prior behavior (no migration to undo, no persisted schema change).
- No worktree or external resource is created; nothing to tear down beyond the branch.

## Self-Review notes (author)

- **Spec coverage:** compose shape + bound (Task 1), normalizer/effective-fragment/net-intent (Task 2), execution resolve + last-wins + single-ref byte-for-byte (Task 3), mount guard + CRASH_DUMP unchanged shape + drift/guard-passes tests + surfaced==enforced constants (Task 4/6), per-ref run-creation validation + empty/over-cap via parse reachable from validate_profile (Task 1/5), surface + Field text + docs regen (Task 6/7). External-lane non-goal: no task (correct — out of scope).
- **Type consistency:** `config_refs`, `effective_config_fragment`, `resolve_config_list_bytes`, `platform_required_payload`, `PLATFORM_REQUIRED_CONFIG`, `REQUIRED_KERNEL_CONFIG`, `PLATFORM_CONFIG_SYMBOL_MISSING` used with identical names/signatures across tasks.
- **validate_profile:** its empty/over-cap rejection is delivered by `BuildProfile.parse` (Task 1), which `runs.validate_profile` already calls — no separate task, matching the spec's "parse + non-empty rejection only" scope.
