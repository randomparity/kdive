# Agent-decidable kdump capability — Implementation Plan (#830)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the write-only `kdump_capable` bit with a computed per-kernel kdump-capability predicate an agent reads from `images.describe`, backed by a build-captured makedumpfile version and a data-driven kernel→makedumpfile rule.

**Architecture:** A new pure module `images/kdump_support.py` owns the rule + predicate (no I/O). The local build plane captures `makedumpfile --version` into `provenance["makedumpfile_version"]` via a marker file written in the existing customize step. The recipe catalog stores `makedumpfile_version` instead of `kdump_capable`. `images.describe` gains an optional `target_kernel` arg and a computed `data.kdump` block.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`. libguestfs (`virt-customize`/`guestfish`) at build time (seam-injected; unit tests use fakes).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-26-kdump-capability-predicate-830.md`. ADR: `docs/adr/0253-kdump-capability-predicate.md`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict (whole tree incl. `tests/`). Absolute imports only.
- ≤100 lines/function, cyclomatic ≤8, ≤5 positional params. Google-style docstrings on non-trivial public APIs.
- Every tool returns a `ToolResponse`; failures carry the most specific `ErrorCategory`; never invent error strings.
- Guardrail before every commit: `just lint && just type && uv run python -m pytest <focused tests> -q`. Full `just ci` before first push.
- Commit trailer required: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- No schema/migration; `provenance` is schemaless jsonb; the catalog field and MCP/CLI surface are additive.
- Branch: `feat/kdump-capability-predicate-830` (already created).

## File Structure

- Create: `src/kdive/images/kdump_support.py` — pure rule + predicate (Task 1).
- Create: `tests/images/test_kdump_support.py` — unit tests for the predicate (Task 1).
- Modify: `src/kdive/images/families/_fedora_customize.py` — shared `makedumpfile_version_marker_args()` (Task 2).
- Modify: `src/kdive/images/families/rhel.py`, `src/kdive/images/families/debian.py` — call the marker helper in the debug branch (Task 2).
- Modify: `src/kdive/images/planes/_build_common.py` — `MakedumpfileProbeSeam` + real marker-read impl (Task 3).
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py` — capture makedumpfile version into provenance (Task 3).
- Modify: `src/kdive/images/rootfs_catalog.py`, `fixtures/local-libvirt/rootfs_catalog.toml` — `makedumpfile_version` field; remove `kdump_capable` (Task 4).
- Modify: `tests/images/test_rootfs_catalog.py` — replace `_MAKEDUMPFILE_BY_NAME`/`_V7_THRESHOLD` guard (Task 4).
- Modify: `src/kdive/mcp/tools/catalog/images.py` — `target_kernel` arg + `data.kdump` block (Task 5).
- Modify: `tests/mcp/test_images_tools.py` (or the existing describe test file) — describe kdump-block tests (Task 5).
- Modify: `src/kdive/cli/commands/registry.py` — `--target-kernel` option on `images describe` (Task 6).
- Regenerate: `docs/guide/reference/images.md` via `just docs` (Task 6).

---

### Task 1: Pure support-matrix + predicate module

**Files:**
- Create: `src/kdive/images/kdump_support.py`
- Test: `tests/images/test_kdump_support.py`

**Interfaces:**
- Produces:
  - `class MakedumpfileVersion` — `parse(s: str) -> MakedumpfileVersion` (extracts a dotted triple from anywhere in `s`, e.g. `"makedumpfile: version 1.7.9 (released ...)"` → `(1,7,9)`; raises `ValueError` if no triple/pair found), total ordering.
  - `class KernelVersion` — `parse(s: str) -> KernelVersion` (leading `major[.minor]`, missing minor → 0, ignores any `.patch`/`-rc`/`+local`/`-gNNN` suffix; raises `ValueError` if no leading integer), total ordering on `(major, minor)`.
  - `SUPPORT_MATRIX: tuple[tuple[KernelVersion, MakedumpfileVersion], ...]`, `KNOWN_THROUGH: KernelVersion`, `DEFAULT_KERNEL_BASIS: KernelVersion`, `MAX_CHARACTERIZED_REQUIREMENT: MakedumpfileVersion`, `MAKEDUMPFILE_CHANGELOG_URL: str`.
  - `required_makedumpfile(kernel: KernelVersion) -> MakedumpfileVersion | None`.
  - `class KdumpCapability` (frozen): `status: str`, `target_kernel: str`, `makedumpfile_version: str | None`, `min_makedumpfile_required: str | None`, `note: str`.
  - `kdump_capability(*, makedumpfile_version: str | None, target_kernel: KernelVersion, kdump_tooling: bool) -> KdumpCapability`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/images/test_kdump_support.py
"""Tests for the pure kdump support-matrix + capability predicate (ADR-0253)."""

from __future__ import annotations

import pytest

from kdive.images.kdump_support import (
    DEFAULT_KERNEL_BASIS,
    KNOWN_THROUGH,
    KernelVersion,
    MakedumpfileVersion,
    kdump_capability,
    required_makedumpfile,
)


def test_makedumpfile_parse_from_version_banner() -> None:
    assert MakedumpfileVersion.parse("makedumpfile: version 1.7.9 (released 2026-04-20)") == (
        MakedumpfileVersion(1, 7, 9)
    )


def test_makedumpfile_parse_bare_triple() -> None:
    assert MakedumpfileVersion.parse("1.7.8") == MakedumpfileVersion(1, 7, 8)


def test_makedumpfile_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        MakedumpfileVersion.parse("not-a-version")


def test_kernel_parse_major_minor_ignores_suffix() -> None:
    assert KernelVersion.parse("7.0.5") == KernelVersion(7, 0)
    assert KernelVersion.parse("7.1.0-rc2") == KernelVersion(7, 1)
    assert KernelVersion.parse("7.0.0-00123-gdeadbee+") == KernelVersion(7, 0)
    assert KernelVersion.parse("7") == KernelVersion(7, 0)


def test_kernel_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        KernelVersion.parse("vanilla")


def test_default_basis_is_known_through() -> None:
    assert DEFAULT_KERNEL_BASIS == KNOWN_THROUGH == KernelVersion(7, 0)


def test_required_makedumpfile_at_known_through() -> None:
    assert required_makedumpfile(KernelVersion(7, 0)) == MakedumpfileVersion(1, 7, 9)


def test_required_makedumpfile_below_matrix_is_none() -> None:
    assert required_makedumpfile(KernelVersion(6, 5)) is None


def test_no_kdump_tooling_is_not_applicable() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.9", target_kernel=KernelVersion(7, 0), kdump_tooling=False
    )
    assert cap.status == "not_applicable"


def test_missing_version_is_unverified() -> None:
    cap = kdump_capability(
        makedumpfile_version=None, target_kernel=KernelVersion(7, 0), kdump_tooling=True
    )
    assert cap.status == "unverified"


def test_unparseable_version_is_unverified() -> None:
    cap = kdump_capability(
        makedumpfile_version="weird", target_kernel=KernelVersion(7, 0), kdump_tooling=True
    )
    assert cap.status == "unverified"


def test_capable_at_known_through() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.9", target_kernel=KernelVersion(7, 0), kdump_tooling=True
    )
    assert cap.status == "capable"
    assert cap.min_makedumpfile_required == "1.7.9"


def test_incapable_at_known_through() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.8", target_kernel=KernelVersion(7, 0), kdump_tooling=True
    )
    assert cap.status == "incapable"


def test_seven_zero_point_release_stays_known() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.9", target_kernel=KernelVersion.parse("7.0.5"), kdump_tooling=True
    )
    assert cap.status == "capable"


def test_newer_kernel_is_unverified_with_changelog() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.9", target_kernel=KernelVersion(7, 1), kdump_tooling=True
    )
    assert cap.status == "unverified"
    assert cap.min_makedumpfile_required is None
    assert "ChangeLog" in cap.note


def test_older_kernel_capable_when_meets_max_characterized() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.9", target_kernel=KernelVersion(6, 5), kdump_tooling=True
    )
    assert cap.status == "capable"


def test_older_kernel_unverified_when_below_max_characterized() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.2", target_kernel=KernelVersion(6, 5), kdump_tooling=True
    )
    assert cap.status == "unverified"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/images/test_kdump_support.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the module**

Implement `src/kdive/images/kdump_support.py`:
- `@dataclass(frozen=True, slots=True, order=True)` for `MakedumpfileVersion(major, minor, patch)` and `KernelVersion(major, minor)`.
- `MakedumpfileVersion.parse`: `re.search(r"(\d+)\.(\d+)\.(\d+)", s)`; if no match, `re.search(r"(\d+)\.(\d+)", s)` → patch 0; else `raise ValueError(f"unrecognized makedumpfile version: {s!r}")`.
- `KernelVersion.parse`: `re.match(r"(\d+)(?:\.(\d+))?", s.strip())`; group2 → minor or 0; no match → `raise ValueError(...)`.
- Constants: `SUPPORT_MATRIX = ((KernelVersion(7, 0), MakedumpfileVersion(1, 7, 9)),)`; `KNOWN_THROUGH = SUPPORT_MATRIX[-1][0]`; `DEFAULT_KERNEL_BASIS = KNOWN_THROUGH`; `MAX_CHARACTERIZED_REQUIREMENT = SUPPORT_MATRIX[-1][1]`; `MAKEDUMPFILE_CHANGELOG_URL = "https://github.com/makedumpfile/makedumpfile/blob/master/ChangeLog"`.
- `required_makedumpfile(kernel)`: iterate `SUPPORT_MATRIX` descending, return first `mdf` where `row_kernel <= kernel`; else `None`.
- `kdump_capability(...)`: follow the spec §1 branch order exactly:
  1. `not kdump_tooling` → `not_applicable` (note `""`, versions echoed as given/None).
  2. parse `makedumpfile_version`; `None` or `ValueError` → `unverified` (note names the cause).
  3. `target_kernel > KNOWN_THROUGH` → `unverified`, `min=None`, note with `MAKEDUMPFILE_CHANGELOG_URL`.
  4. `req = required_makedumpfile(target_kernel)`:
     - `req is not None` → `capable` if `mdf >= req` else `incapable`; `min = str(req)`.
     - `req is None` → `capable` if `mdf >= MAX_CHARACTERIZED_REQUIREMENT` else `unverified` (note: requirement for this older kernel not characterized + ChangeLog URL).
- Add a `__str__`/helper rendering `MakedumpfileVersion`→`"1.7.9"` and `KernelVersion`→`"7.0"` for the result fields and notes.
- Keep `kdump_capability` ≤8 complexity: extract the older-than-matrix branch into a small helper if needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/images/test_kdump_support.py -q`
Expected: PASS (all).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/images/test_kdump_support.py -q
git add src/kdive/images/kdump_support.py tests/images/test_kdump_support.py
git commit -m "feat(images): add pure kdump support-matrix + capability predicate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Write the makedumpfile-version marker during build customization

**Files:**
- Modify: `src/kdive/images/families/_fedora_customize.py`
- Modify: `src/kdive/images/families/rhel.py:86-` (debug branch of `customize_argv`)
- Modify: `src/kdive/images/families/debian.py:138-140` (debug branch of `customize_argv`)
- Test: `tests/images/test_families.py` (or the existing family-argv test module — locate with `rg -l "customize_argv" tests/`)

**Interfaces:**
- Produces: `makedumpfile_version_marker_args() -> list[str]` in `_fedora_customize.py` — a virt-customize fragment that ensures `/usr/lib/kdive/` exists and writes `makedumpfile --version` into `MAKEDUMPFILE_MARKER_GUEST_PATH = "/usr/lib/kdive/makedumpfile-version"`. Consumed by both debug families.

- [ ] **Step 1: Write the failing test**

```python
# in the family-argv test module
from kdive.images.families._fedora_customize import (
    MAKEDUMPFILE_MARKER_GUEST_PATH,
    makedumpfile_version_marker_args,
)


def test_makedumpfile_marker_args_writes_version_file() -> None:
    argv = makedumpfile_version_marker_args()
    joined = " ".join(argv)
    assert "--run-command" in argv
    assert MAKEDUMPFILE_MARKER_GUEST_PATH in joined
    assert "makedumpfile --version" in joined


def test_rhel_debug_argv_includes_makedumpfile_marker() -> None:
    # build a debug CustomizeContext for rhel and assert the marker path appears in customize_argv
    # (mirror the existing rhel debug-argv test's context construction)
    ...
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest <family test module> -q`
Expected: FAIL (symbol not defined).

- [ ] **Step 3: Implement**

In `_fedora_customize.py`:
```python
MAKEDUMPFILE_MARKER_GUEST_PATH = "/usr/lib/kdive/makedumpfile-version"


def makedumpfile_version_marker_args() -> list[str]:
    """virt-customize fragment recording ``makedumpfile --version`` to a guest marker file.

    Read back at build time into ``provenance["makedumpfile_version"]`` (ADR-0253). Best-effort:
    the command never fails the build (``|| true``); an image without makedumpfile leaves an empty
    marker, which the probe treats as "absent".
    """
    return [
        "--run-command",
        "mkdir -p /usr/lib/kdive && "
        f"makedumpfile --version > {MAKEDUMPFILE_MARKER_GUEST_PATH} 2>/dev/null || true",
    ]
```
In `rhel.py` and `debian.py`, inside the `if ctx.kind == "debug":` branch (next to `drgn_helper_args()`), append `makedumpfile_version_marker_args()`. Add the import.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest <family test module> -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/images/ -q
git add src/kdive/images/families/_fedora_customize.py src/kdive/images/families/rhel.py src/kdive/images/families/debian.py tests/
git commit -m "feat(images): write makedumpfile --version marker during debug build

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Capture the makedumpfile version into provenance (probe seam + fallback)

**Files:**
- Modify: `src/kdive/images/planes/_build_common.py:152-213` (after the version-inspect seam)
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py:155-165` (tools dataclass), `:260-275` (build), `:325-360` (`_provenance`)
- Test: `tests/providers/local_libvirt/test_rootfs_build.py` (locate with `rg -l "RootfsBuildTools\|_provenance" tests/`)

**Interfaces:**
- Consumes: `MakedumpfileVersion.parse` (Task 1), `MAKEDUMPFILE_MARKER_GUEST_PATH` (Task 2).
- Produces:
  - `_build_common.MakedumpfileProbeSeam = Callable[[Path], str | None]` and `DEFAULT_MAKEDUMPFILE_PROBE` (real impl: read the marker via read-only `guestfish`, return the raw line or `None`).
  - `RootfsBuildTools.probe_makedumpfile: MakedumpfileProbeSeam = DEFAULT_MAKEDUMPFILE_PROBE`.
  - `provenance["makedumpfile_version"]` (str) added when resolvable.

- [ ] **Step 1: Write failing tests** (drive the plane with fakes; no libguestfs)

```python
def test_provenance_records_makedumpfile_version_from_probe() -> None:
    tools = _debug_tools(  # existing fake-tools helper in this test module
        probe_makedumpfile=lambda _scratch: "makedumpfile: version 1.7.9 (released ...)",
    )
    out = LocalLibvirtRootfsBuildPlane(workspace=..., tools=tools).build(_debug_spec())
    assert out.provenance["makedumpfile_version"] == "1.7.9"


def test_provenance_falls_back_to_package_versions_when_probe_empty() -> None:
    tools = _debug_tools(
        probe_makedumpfile=lambda _scratch: None,
        inspect_versions=lambda _scratch: {"makedumpfile": "1.7.2", "qemu-guest-agent": "9.0"},
    )
    out = LocalLibvirtRootfsBuildPlane(workspace=..., tools=tools).build(_debug_spec())
    assert out.provenance["makedumpfile_version"] == "1.7.2"


def test_provenance_omits_makedumpfile_version_when_both_empty() -> None:
    tools = _debug_tools(
        probe_makedumpfile=lambda _scratch: None,
        inspect_versions=lambda _scratch: {},
    )
    out = LocalLibvirtRootfsBuildPlane(workspace=..., tools=tools).build(_debug_spec())
    assert "makedumpfile_version" not in out.provenance


def test_provenance_omits_on_probe_error() -> None:
    def boom(_scratch):
        raise CategorizedError("nope", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    tools = _debug_tools(probe_makedumpfile=boom, inspect_versions=lambda _s: {})
    out = LocalLibvirtRootfsBuildPlane(workspace=..., tools=tools).build(_debug_spec())
    assert "makedumpfile_version" not in out.provenance
```

If the test module has no fake-tools helper that accepts these seams, extend it minimally (mirror how `inspect_versions` is already faked in the ADR-0252 tests).

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_rootfs_build.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`_build_common.py`:
```python
type MakedumpfileProbeSeam = Callable[[Path], str | None]


def _real_makedumpfile_probe(qcow2_path: Path) -> str | None:  # pragma: no cover - live_vm
    """Read the build-written ``/usr/lib/kdive/makedumpfile-version`` marker, read-only."""
    argv = ["guestfish", "--ro", "-a", str(qcow2_path), "-i", "cat", MAKEDUMPFILE_MARKER_GUEST_PATH]
    # run via the same subprocess pattern as _real_inspect; on FileNotFound -> MISSING_DEPENDENCY,
    # timeout -> INFRASTRUCTURE_FAILURE, non-zero (marker absent) -> return None.
    ...
    return stripped_stdout or None


DEFAULT_MAKEDUMPFILE_PROBE: MakedumpfileProbeSeam = _real_makedumpfile_probe
```
Import `MAKEDUMPFILE_MARKER_GUEST_PATH` from `_fedora_customize` (or relocate the constant to `_build_common` and re-export to avoid a families→build import cycle — prefer defining it in `_build_common` and importing into `_fedora_customize`).

`rootfs_build.py`: add `probe_makedumpfile: MakedumpfileProbeSeam = DEFAULT_MAKEDUMPFILE_PROBE` to `RootfsBuildTools`; add `_capture_makedumpfile(self, scratch, package_versions) -> str | None`:
```python
def _capture_makedumpfile(self, scratch: Path, package_versions: dict[str, str]) -> str | None:
    try:
        raw = self._tools.probe_makedumpfile(scratch)
    except CategorizedError:
        _log.warning("makedumpfile probe failed; trying package_versions fallback", exc_info=True)
        raw = None
    if raw:
        try:
            return str(MakedumpfileVersion.parse(raw))
        except ValueError:
            _log.warning("makedumpfile marker %r did not parse; provenance omits it", raw)
    fallback = package_versions.get("makedumpfile")
    if fallback:
        try:
            return str(MakedumpfileVersion.parse(fallback))
        except ValueError:
            return None
    return None
```
In `build()`, after `package_versions = self._capture_versions(...)`, compute
`makedumpfile_version = self._capture_makedumpfile(scratch, package_versions)` and pass it to
`_provenance(...)`. In `_provenance`, add `if makedumpfile_version: record["makedumpfile_version"] = makedumpfile_version`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_rootfs_build.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/providers/local_libvirt/ tests/images/ -q
git add -A
git commit -m "feat(images): capture makedumpfile version into build provenance

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Recipe catalog records makedumpfile_version, not kdump_capable

**Files:**
- Modify: `src/kdive/images/rootfs_catalog.py:42-111` (entry + parse), `src/kdive/providers/local_libvirt/rootfs_build.py:176-190` (synthesized fallback row)
- Modify: `fixtures/local-libvirt/rootfs_catalog.toml` (every row + header comment)
- Test: `tests/images/test_rootfs_catalog.py`

**Interfaces:**
- Consumes: `kdump_capability`, `KernelVersion`, `DEFAULT_KERNEL_BASIS` (Task 1).
- Produces: `RootfsCatalogEntry.makedumpfile_version: str` (no more `kdump_capable`).

- [ ] **Step 1: Update the tests first**

Remove `_MAKEDUMPFILE_BY_NAME`/`_V7_THRESHOLD` and the two `kdump_capable` guard tests. Add:
```python
_EXPECTED_MAKEDUMPFILE: dict[str, str] = {
    "fedora-kdive-ready-44": "1.7.9",
    "fedora-kdive-ready-43": "1.7.8",
    "rocky-kdive-ready-10": "1.7.8",
    "rocky-kdive-ready-9": "1.7.6",
    "rocky-kdive-ready-8": "1.7.2",
    "centos-stream-kdive-ready-10": "1.7.8",
    "centos-stream-kdive-ready-9": "1.7.6",
    "debian-kdive-ready-12": "1.7.2",
    "debian-kdive-ready-13": "1.7.6",
}


def test_catalog_makedumpfile_versions_match_snapshot() -> None:
    cat = load_rootfs_catalog()
    for name, version in _EXPECTED_MAKEDUMPFILE.items():
        assert cat[name].makedumpfile_version == version, name


def test_only_fedora_44_is_capable_for_default_basis() -> None:
    cat = load_rootfs_catalog()
    for name in _EXPECTED_MAKEDUMPFILE:
        cap = kdump_capability(
            makedumpfile_version=cat[name].makedumpfile_version,
            target_kernel=DEFAULT_KERNEL_BASIS,
            kdump_tooling=True,
        )
        expected = "capable" if name == "fedora-kdive-ready-44" else "incapable"
        assert cap.status == expected, name
```
Update the two config-error fixture tests that currently assert on `kdump_capable`: replace the missing-`kdump_capable` test with a missing-`makedumpfile_version` test (field name `"makedumpfile_version"`), and drop the non-bool test (the field is now a string — add a missing-field test instead).

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/images/test_rootfs_catalog.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`rootfs_catalog.py`: drop the `kdump_capable: bool` attribute + its docstring; add `makedumpfile_version: str`; in `_parse_entry` replace `kdump_capable=_require_bool(row, "kdump_capable")` with `makedumpfile_version=_require_str(row, "makedumpfile_version")`. Update the class docstring to describe the new field (curated per-release snapshot; predicate lives in `kdump_support`).
`rootfs_build.py` `_resolve_entry` fallback: replace `kdump_capable=False` with `makedumpfile_version=""`.
`rootfs_catalog.toml`: for every `[[image]]`, replace the `kdump_capable = ...  # comment` line with `makedumpfile_version = "X.Y.Z"` (values from `_EXPECTED_MAKEDUMPFILE`), keeping a short trailing comment where useful. Update the header comment block to describe `makedumpfile_version` + point at `images.kdump_support` for the predicate.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/images/test_rootfs_catalog.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/images/ tests/providers/local_libvirt/ -q
git add -A
git commit -m "feat(images): store makedumpfile_version in rootfs catalog, drop kdump_capable

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: images.describe computes the data.kdump block + target_kernel arg

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/images.py:131-227`
- Test: the existing describe test module (locate: `rg -l "describe_image\|images.describe" tests/`)

**Interfaces:**
- Consumes: `kdump_capability`, `KernelVersion`, `DEFAULT_KERNEL_BASIS` (Task 1); `_invalid_uuid_error`/config-error helpers already imported in `images.py`.
- Produces: `describe_image(pool, ctx, image_id, target_kernel: str | None = None)`; `data["kdump"]` block.

- [ ] **Step 1: Write failing tests** (drive `describe_image` directly with a seeded row)

```python
async def test_describe_kdump_block_capable_default_basis(...) -> None:
    # seed a registered public debug image with provenance.makedumpfile_version = "1.7.9"
    # and capabilities including "kdump"
    resp = await describe_image(pool, ctx, image_id)
    k = resp.structured_content["data"]["kdump"]
    assert k["capability"] == "capable"
    assert k["target_kernel"] == "7.0"
    assert k["makedumpfile_version"] == "1.7.9"


async def test_describe_kdump_block_unverified_for_newer_kernel(...) -> None:
    resp = await describe_image(pool, ctx, image_id, target_kernel="7.1")
    k = resp.structured_content["data"]["kdump"]
    assert k["capability"] == "unverified"
    assert "ChangeLog" in k["note"]


async def test_describe_kdump_not_applicable_for_build_image(...) -> None:
    # capabilities = ["agent", "build"] (no kdump)
    resp = await describe_image(pool, ctx, image_id)
    assert resp.structured_content["data"]["kdump"]["capability"] == "not_applicable"


async def test_describe_malformed_target_kernel_is_config_error(...) -> None:
    resp = await describe_image(pool, ctx, image_id, target_kernel="vanilla")
    assert resp.structured_content["error_category"] == "configuration_error"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest <describe test module> -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `images.py`:
- `describe_image` gains `target_kernel: str | None = None`. Before the DB read, if `target_kernel` is not None, parse it with `KernelVersion.parse`; on `ValueError` return a `configuration_error` naming `target_kernel` (reuse the existing config-error helper shape used for malformed inputs in this package). Otherwise basis = `DEFAULT_KERNEL_BASIS`.
- In `_describe_envelope`, add a `_kdump_block(entry, basis)` helper:
```python
def _kdump_block(entry: ImageCatalogEntry, basis: KernelVersion) -> dict[str, object]:
    raw = entry.provenance.get("makedumpfile_version")
    cap = kdump_capability(
        makedumpfile_version=raw if isinstance(raw, str) and raw else None,
        target_kernel=basis,
        kdump_tooling="kdump" in entry.capabilities,
    )
    return {
        "makedumpfile_version": raw if isinstance(raw, str) else "",
        "target_kernel": cap.target_kernel,
        "capability": cap.status,
        "min_makedumpfile_required": cap.min_makedumpfile_required,
        "note": cap.note,
    }
```
- Pass `basis` from `describe_image` into `_describe_envelope`; add `"kdump": _kdump_block(entry, basis)` to `data`.
- The `@app.tool` `images_describe` wrapper gains an `Annotated[str | None, Field(description="Target kernel version (e.g. 7.1) to compute kdump capability against; defaults to the characterized basis.")] = None` parameter, forwarded to `describe_image`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest <describe test module> -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/mcp/ -q
git add -A
git commit -m "feat(mcp): compute kdump capability block in images.describe

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: CLI --target-kernel + regenerate tool reference

**Files:**
- Modify: `src/kdive/cli/commands/registry.py` (the `images describe` Verb)
- Regenerate: `docs/guide/reference/images.md` (via `just docs`)
- Test: the CLI registry/verbs test module (locate: `rg -l "images.*describe\|Verb(" tests/cli`)

**Interfaces:**
- Consumes: the `images.describe` tool's new `target_kernel` arg (Task 5).

- [ ] **Step 1: Write the failing test**

Assert the `images describe` verb declares a `target_kernel` option mapped to the tool arg (mirror an existing `options=(...)` verb test such as `resources list`/`kind`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/cli/ -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `registry.py`, change the `images describe` Verb to add `options=("target_kernel",)` (the generic single-record path already supports `options=`, e.g. `resources list`). Confirm the generic path forwards an absent option as `None` (it does for other optional options).

- [ ] **Step 4: Run + regenerate docs**

```bash
uv run python -m pytest tests/cli/ -q     # PASS
just docs                                  # regenerate docs/guide/reference/*.md
just docs-check                            # PASS (committed reference matches)
```

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/cli/ -q && just docs-check
git add -A
git commit -m "feat(cli): add --target-kernel to images describe; regen tool reference

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Full suite + live proof of build-time capture

**Files:** none (verification).

- [ ] **Step 1: Full local gate**

Run: `just ci`
Expected: PASS (or only hardware/credential-gated jobs skipped). Fix any architecture/doc/snapshot test that a full run surfaces.

- [ ] **Step 2: Live proof (this host runs KVM/libvirt; `live_vm`)**

Build a Fedora image and an EL8/EL9 image via `build-fs --image ...`, then confirm the published image's catalog `provenance["makedumpfile_version"]` is populated and correct — the marker path on Fedora (standalone makedumpfile) and the bundled-`kexec-tools` case on EL. Record the observed values in the PR body. If the marker probe yields nothing on a family, capture why (e.g. makedumpfile path) and confirm the `package_versions` fallback or the honest `unverified` degrade behaves as specified — do **not** loosen the predicate to force a `capable`.

- [ ] **Step 3: Record outcome**

Note the live results (versions seen, families exercised) for the PR body; this is the gate the spec requires before merge.

---

## Self-Review

- **Spec coverage:** §1→Task 1; §2→Tasks 2-3 + Task 7 live proof; §3→Task 4; §4→Tasks 5-6. All five acceptance criteria map to a task. ✓
- **Placeholders:** the `...` markers in Tasks 3/5 test snippets are seed/fixture boilerplate the implementer fills from the neighboring existing tests; the implementation steps carry full code. Probe `_real_*` bodies reference the existing `_real_inspect` subprocess pattern in the same file. ✓
- **Type consistency:** `MakedumpfileProbeSeam`, `makedumpfile_version` (str), `KdumpCapability.status/target_kernel/makedumpfile_version/min_makedumpfile_required/note`, `MAKEDUMPFILE_MARKER_GUEST_PATH`, `kdump_capability(*, makedumpfile_version, target_kernel, kdump_tooling)` are used identically across tasks. ✓
- **Import-cycle note:** `MAKEDUMPFILE_MARKER_GUEST_PATH` is defined in `_build_common.py` and imported by `_fedora_customize.py` to avoid a families→build cycle (Task 3 step 3). ✓
