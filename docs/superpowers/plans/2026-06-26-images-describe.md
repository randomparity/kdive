# images.describe + build-time package-version provenance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `images.describe(image_id)` MCP tool (+ `kdivectl images describe`) that returns one catalog row's full agent-relevant detail, and capture installed package versions at build time into `provenance["package_versions"]`.

**Architecture:** Two loosely-coupled halves joined by the `provenance` jsonb blob. The build half adds a shared `virt-inspector` version-inspection seam injected into both build planes, writing an additive `package_versions` map. The read half surfaces the whole row (provenance verbatim) addressed by row id UUID, reusing the `images.list` RBAC predicate with a no-leak `not_found`.

**Tech Stack:** Python 3.14, `uv`, `psycopg` (async, `dict_row`), FastMCP, libguestfs (`virt-inspector`), pytest. Guardrails: `just lint`, `just type`, `just test`, `just docs-check`.

## Global Constraints

- Spec: `docs/specs/2026-06-26-images-describe.md`. ADR: `docs/adr/0252-images-describe.md`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, whole-tree (src + tests).
- Absolute imports only (`kdive.…`), no relative imports.
- Every MCP tool returns a `ToolResponse`; a failure envelope carries the most specific `ErrorCategory`. Never invent error strings.
- `ToolResponse.data` is `dict[str, JsonValue]`; `JsonValue` excludes `datetime` (serialize via `.isoformat()`).
- Read tools carry `annotations=_docmeta.read_only()`, `meta={"maturity": "implemented"}`.
- Version capture is **degrade-don't-fail**: a `CategorizedError` from the inspector logs a WARNING and omits `package_versions`; the build still succeeds.
- `provenance["packages"]` (a `list[str]`) is **unchanged**; `package_versions` is a separate additive field.
- Guardrail before every commit: `just lint && just type` plus the focused tests named in the task. Run `just test` (full) once before push.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Shared package-version inspection seam

**Files:**
- Modify: `src/kdive/images/planes/_build_common.py`
- Test: `tests/images/planes/test_build_common_versions.py` (create)

**Interfaces:**
- Produces:
  - `type VersionInspectSeam = Callable[[Path], dict[str, str]]` — full installed `{name: version}` map for a qcow2.
  - `parse_virt_inspector_versions(xml: str) -> dict[str, str]` — pure parser (entity resolution disabled).
  - `inspect_package_versions(qcow2_path: Path) -> dict[str, str]` — real `virt-inspector` seam (live; `# pragma: no cover - live_vm`).
  - `DEFAULT_VERSION_INSPECT: VersionInspectSeam = inspect_package_versions`.

- [ ] **Step 1: Write the failing test** (`tests/images/planes/test_build_common_versions.py`)

```python
"""parse_virt_inspector_versions: pure XML -> {name: version}."""

from __future__ import annotations

import pytest

from kdive.images.planes._build_common import parse_virt_inspector_versions

_XML = """<?xml version="1.0"?>
<operatingsystems>
  <operatingsystem>
    <name>linux</name>
    <applications>
      <application><name>makedumpfile</name><version>1.7.9</version></application>
      <application><name>drgn</name><version>0.0.28</version><release>1.fc44</release></application>
      <application><name>nameless</name></application>
    </applications>
  </operatingsystem>
</operatingsystems>"""


def test_parse_maps_name_to_version() -> None:
    assert parse_virt_inspector_versions(_XML) == {
        "makedumpfile": "1.7.9",
        "drgn": "0.0.28",
    }


def test_parse_skips_application_without_version() -> None:
    assert "nameless" not in parse_virt_inspector_versions(_XML)


def test_parse_empty_or_no_applications_is_empty() -> None:
    assert parse_virt_inspector_versions("<operatingsystems/>") == {}


def test_parse_rejects_doctype_entities() -> None:
    # Defensive: an external/general entity must not be expanded (no billion-laughs).
    hostile = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]>'
        "<operatingsystems><operatingsystem><applications>"
        "<application><name>&e;</name><version>1</version></application>"
        "</applications></operatingsystem></operatingsystems>"
    )
    with pytest.raises(ValueError):
        parse_virt_inspector_versions(hostile)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/images/planes/test_build_common_versions.py -q`
Expected: FAIL with `ImportError: cannot import name 'parse_virt_inspector_versions'`.

- [ ] **Step 3: Write minimal implementation** (append to `src/kdive/images/planes/_build_common.py`)

Add imports at the top of the file (keep alphabetical with existing imports):

```python
from collections.abc import Callable
from xml.etree.ElementTree import fromstring as _xml_fromstring
```

Add the seam (after the existing helpers; reuse the module's `subprocess`, `CategorizedError`, `ErrorCategory`):

```python
_VIRT_INSPECTOR_TIMEOUT_S = 5 * 60

type VersionInspectSeam = Callable[[Path], dict[str, str]]


def parse_virt_inspector_versions(xml: str) -> dict[str, str]:
    """Map each ``<application>`` with a ``<name>`` and ``<version>`` to ``{name: version}``.

    Applications missing a name or version are skipped. A DOCTYPE is rejected up front so a
    crafted package name cannot trigger entity expansion (stdlib ElementTree expands internal
    entities only when a DTD is present).
    """
    if "<!DOCTYPE" in xml:
        raise ValueError("DOCTYPE is not allowed in virt-inspector output")
    root = _xml_fromstring(xml)
    versions: dict[str, str] = {}
    for app in root.iter("application"):
        name = app.findtext("name")
        version = app.findtext("version")
        if name and version:
            versions[name] = version
    return versions


def inspect_package_versions(qcow2_path: Path) -> dict[str, str]:  # pragma: no cover - live_vm
    """Return the full installed ``{name: version}`` map for ``qcow2_path`` via ``virt-inspector``.

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` if ``virt-inspector`` is absent;
            ``INFRASTRUCTURE_FAILURE`` on timeout or a non-zero exit.
    """
    argv = ["virt-inspector", "--no-icon", "-a", str(qcow2_path)]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv; image path is a data arg
            argv, capture_output=True, text=True, timeout=_VIRT_INSPECTOR_TIMEOUT_S, check=False
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            "virt-inspector is not installed; cannot capture package versions",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": "virt-inspector"},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "virt-inspector exceeded its timeout",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": _VIRT_INSPECTOR_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "virt-inspector failed",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": result.stderr[-2000:]},
        )
    return parse_virt_inspector_versions(result.stdout)


DEFAULT_VERSION_INSPECT: VersionInspectSeam = inspect_package_versions
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/images/planes/test_build_common_versions.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/images/planes/_build_common.py tests/images/planes/test_build_common_versions.py
git commit -m "feat(images): add virt-inspector package-version seam

Refs #829

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Capture versions in the local build plane

**Files:**
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py`
- Test: `tests/providers/local_libvirt/test_rootfs_build.py:30-235` (existing provenance tests)

**Interfaces:**
- Consumes: `VersionInspectSeam`, `DEFAULT_VERSION_INSPECT` (Task 1).
- Produces: `RootfsBuildTools.inspect_versions: VersionInspectSeam` field; `provenance["package_versions"]` (dict, omitted on degrade).

- [ ] **Step 1: Write the failing test** (add to `tests/providers/local_libvirt/test_rootfs_build.py`)

The existing `_Recorder`/`_plane` fixtures inject a `RootfsBuildTools`. Add a fake version
seam to the recorder and assert provenance. First read the file to match the fixture style; then
add:

```python
def test_provenance_records_package_versions(tmp_path: Path) -> None:
    rec = _Recorder(authorized_key=_key(tmp_path))
    # _spec() requests packages ("openssh-server", "drgn"); the fake inspector reports a superset.
    versions = {"openssh-server": "9.6", "drgn": "0.0.28", "glibc": "2.39"}
    out = _plane(tmp_path, rec, inspect_versions=lambda _q: versions).build(_spec())
    # Filtered to the requested set; the unrequested glibc is dropped.
    assert out.provenance["package_versions"] == {"openssh-server": "9.6", "drgn": "0.0.28"}
    assert out.provenance["packages"] == ["openssh-server", "drgn"]  # unchanged


def test_provenance_omits_versions_on_inspector_failure(tmp_path: Path) -> None:
    from kdive.domain.errors import CategorizedError, ErrorCategory

    def _boom(_q: Path) -> dict[str, str]:
        raise CategorizedError("no tool", category=ErrorCategory.MISSING_DEPENDENCY)

    rec = _Recorder(authorized_key=_key(tmp_path))
    out = _plane(tmp_path, rec, inspect_versions=_boom).build(_spec())
    assert "package_versions" not in out.provenance


def test_provenance_versions_absent_for_unreported_request(tmp_path: Path) -> None:
    rec = _Recorder(authorized_key=_key(tmp_path))
    out = _plane(tmp_path, rec, inspect_versions=lambda _q: {"drgn": "0.0.28"}).build(_spec())
    pv = out.provenance["package_versions"]
    assert pv == {"drgn": "0.0.28"}
    assert "openssh-server" in out.provenance["packages"]  # requested, just no version reported
```

Update the existing `_plane` helper to accept and thread `inspect_versions` (default a fake
returning `{}`), and update the two full-dict provenance assertions
(`test_provenance_source_digest_for_virt_builder_entry`,
`test_provenance_source_digest_for_cloud_image_entry`) to expect `"package_versions": {}` (their
`_plane` uses the default empty fake).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_rootfs_build.py -q`
Expected: FAIL (new tests reference `inspect_versions`; `RootfsBuildTools` has no such field).

- [ ] **Step 3: Write minimal implementation** (`src/kdive/providers/local_libvirt/rootfs_build.py`)

Add imports:

```python
import logging
from kdive.images.planes._build_common import DEFAULT_VERSION_INSPECT, VersionInspectSeam
```

Add `_log = logging.getLogger(__name__)` near the module constants.

Add the seam to `RootfsBuildTools`:

```python
    inspect_versions: VersionInspectSeam = DEFAULT_VERSION_INSPECT
```

In `build()`, after `family.normalize(staged)` and before `publish_qcow2`, capture from the
customized `scratch` (a normal bootable OS disk, still present in the workspace):

```python
            family.normalize(staged)
            package_versions = self._capture_versions(scratch, spec.packages)
            qcow2 = publish_qcow2(self._workspace, image_name=spec.name, scratch=staged)
```

Thread `package_versions` into `_provenance(... , package_versions=package_versions)`.

Add the degrade helper:

```python
    def _capture_versions(self, scratch: Path, requested: tuple[str, ...]) -> dict[str, str]:
        """Installed versions for the requested packages; ``{}`` (logged) on inspector failure."""
        try:
            installed = self._tools.inspect_versions(scratch)
        except CategorizedError:
            _log.warning("package-version capture failed; provenance omits package_versions",
                         exc_info=True)
            return {}
        return {name: installed[name] for name in requested if name in installed}
```

Update `_provenance` to take `package_versions: dict[str, str]` and add it **only when
non-empty** (so a degraded build omits the key):

```python
def _provenance(
    spec: RootfsBuildSpec,
    entry: RootfsCatalogEntry,
    family: FamilyCustomizer,
    *,
    size: str,
    authorized_key: Path,
    package_versions: dict[str, str],
) -> dict[str, object]:
    record: dict[str, object] = {
        "plane": "local-libvirt",
        "distro": spec.distro,
        "releasever": spec.releasever,
        "packages": list(spec.packages),
        "source_image_digest": _source_digest(entry.source),
        "capabilities": list(spec.capabilities),
        "arch": spec.arch,
        "image_size": size,
        "authorized_key_name": authorized_key.name,
        "readiness_marker": _READINESS_MARKER,
        "layout": "whole-disk-ext4-qcow2",
        "guest_mac": family.guest_mac,
    }
    if package_versions:
        record["package_versions"] = package_versions
    return record
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_rootfs_build.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/providers/local_libvirt/rootfs_build.py tests/providers/local_libvirt/test_rootfs_build.py
git commit -m "feat(images): capture package versions in the local build plane

Refs #829

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Capture versions in the remote build plane

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/rootfs_build.py`
- Test: `tests/providers/remote_libvirt/test_rootfs_build.py` (existing provenance tests)

**Interfaces:**
- Consumes: `VersionInspectSeam`, `DEFAULT_VERSION_INSPECT` (Task 1); `_guest_agent_packages` (existing).
- Produces: `RemoteRootfsBuildTools.inspect_versions`; `provenance["package_versions"]` filtered to `_guest_agent_packages(spec.packages)`.

- [ ] **Step 1: Write the failing test** (read `tests/providers/remote_libvirt/test_rootfs_build.py` first to match its fixture/injection style, then add)

```python
def test_remote_provenance_records_versions_including_guest_agent(tmp_path: Path) -> None:
    # spec requests ("drgn",); remote always injects qemu-guest-agent, so both are captured.
    versions = {"drgn": "0.0.28", "qemu-guest-agent": "9.0", "glibc": "2.39"}
    out = _build(tmp_path, packages=("drgn",), inspect_versions=lambda _q: versions)
    assert out.provenance["package_versions"] == {"drgn": "0.0.28", "qemu-guest-agent": "9.0"}


def test_remote_provenance_omits_versions_on_failure(tmp_path: Path) -> None:
    from kdive.domain.errors import CategorizedError, ErrorCategory

    def _boom(_q: Path) -> dict[str, str]:
        raise CategorizedError("no tool", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    out = _build(tmp_path, packages=("drgn",), inspect_versions=_boom)
    assert "package_versions" not in out.provenance
```

Update the remote test's build helper to thread `inspect_versions` into
`RemoteRootfsBuildTools`, and update any existing full-dict provenance assertion to include
`"package_versions": {}` for the default empty fake.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_rootfs_build.py -q`
Expected: FAIL (`RemoteRootfsBuildTools` has no `inspect_versions`).

- [ ] **Step 3: Write minimal implementation** (`src/kdive/providers/remote_libvirt/rootfs_build.py`)

Mirror Task 2: add `import logging`, `_log`, the `DEFAULT_VERSION_INSPECT`/`VersionInspectSeam`
import, the `inspect_versions` field on `RemoteRootfsBuildTools`, a `_capture_versions` helper
filtering to `_guest_agent_packages(spec.packages)`, and thread `package_versions` into
`_provenance` (add the key only when non-empty). Capture from the `scratch`/`qcow2` virt-builder
output before `publish_qcow2`:

```python
            package_versions = self._capture_versions(scratch, spec.packages)
            qcow2 = publish_qcow2(self._workspace, image_name=spec.name, scratch=scratch)
```

```python
    def _capture_versions(self, qcow2: Path, requested: tuple[str, ...]) -> dict[str, str]:
        try:
            installed = self._tools.inspect_versions(qcow2)
        except CategorizedError:
            _log.warning("package-version capture failed; provenance omits package_versions",
                         exc_info=True)
            return {}
        wanted = _guest_agent_packages(requested)
        return {name: installed[name] for name in wanted if name in installed}
```

`_provenance(spec, *, size, package_versions)` adds `record["package_versions"] =
package_versions` only when non-empty.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_rootfs_build.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/providers/remote_libvirt/rootfs_build.py tests/providers/remote_libvirt/test_rootfs_build.py
git commit -m "feat(images): capture package versions in the remote build plane

Refs #829

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `images.describe` handler + registration

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/images.py`
- Modify: `tests/mcp/test_read_tools_annotated.py:24-39` (add to `READ_TOOLS`)
- Test: `tests/mcp/catalog/test_images_describe.py` (create)

**Interfaces:**
- Consumes: existing `image_catalog` rows; `ImageCatalogEntry`, `projects_with_role`, `Role`.
- Produces: `describe_image(pool, ctx, image_id) -> ToolResponse`; the `images.describe` tool.

- [ ] **Step 1: Write the failing test** (`tests/mcp/catalog/test_images_describe.py`)

Reuse the `_pool`/`_insert`/`_ctx`/`_member_ctx` helpers from `test_images_list.py` (copy them
or import; copy to keep tests self-contained). `_insert` must return the row id — extend it to
`RETURNING id` and return `str(id)`.

```python
async def test_describe_public_row_carries_full_detail(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            iid = await _insert(pool, name="fedora", visibility="public", owner=None)
            resp = await catalog_images.describe_image(pool, _ctx(), iid)
        assert resp.status == "registered"
        d = resp.data
        assert d["name"] == "fedora" and d["format"] == "qcow2"
        assert d["root_device"] == "/dev/vda" and d["digest"] == "sha256:abc"
        assert d["capabilities"] == [] and d["provenance"] == {}
        assert "object_key" not in d and "path" not in d
    asyncio.run(_run())


async def test_describe_owned_private_visible(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            iid = await _insert(pool, name="mine", visibility="private", owner="proj-a")
            resp = await catalog_images.describe_image(pool, _ctx("proj-a"), iid)
        assert resp.status != "error" and resp.data["name"] == "mine"
    asyncio.run(_run())


async def test_describe_unauthorized_private_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            iid = await _insert(pool, name="theirs", visibility="private", owner="proj-b")
            visible = await catalog_images.describe_image(pool, _ctx("proj-a"), iid)
            unknown = await catalog_images.describe_image(
                pool, _ctx("proj-a"), "00000000-0000-0000-0000-000000000000")
        assert visible.status == "error" and visible.error_category == "not_found"
        # No existence leak: byte-identical to a genuinely-unknown id.
        assert visible.model_dump(exclude={"object_id"}) == unknown.model_dump(exclude={"object_id"})
    asyncio.run(_run())


async def test_describe_malformed_id_is_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await catalog_images.describe_image(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"
    asyncio.run(_run())


async def test_describe_withholds_staged_path(migrated_url: str) -> None:
    secret = "/var/lib/kdive/rootfs/secret-local.qcow2"

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            iid = await _insert_staged_path(pool, name="local-rootfs", path=secret)
            resp = await catalog_images.describe_image(pool, _ctx(), iid)
        assert "path" not in resp.data
        assert secret not in str(resp.model_dump())
    asyncio.run(_run())
```

(Wrap each as the file's tests are wrapped — see `test_images_list.py` for the `asyncio.run`
pattern and the `migrated_url` fixture. `_insert`/`_insert_staged_path` must `RETURNING id` and
return `str(id)`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/catalog/test_images_describe.py -q`
Expected: FAIL (`describe_image` undefined).

- [ ] **Step 3: Write minimal implementation** (`src/kdive/mcp/tools/catalog/images.py`)

Add imports:

```python
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
```

Add the describe SQL and handler:

```python
_DESCRIBE_TOOL = "images.describe"

_DESCRIBE_SQL = """
    SELECT *
    FROM image_catalog
    WHERE id = %(id)s
      AND (visibility = %(public)s
           OR (visibility = %(private)s AND owner = ANY(%(projects)s)))
"""


def _describe_envelope(entry: ImageCatalogEntry) -> ToolResponse:
    """Full per-image detail; withholds the staged ``path`` and the S3 ``object_key``."""
    return ToolResponse.success(
        str(entry.id),
        entry.state.value,
        data={
            "provider": entry.provider,
            "name": entry.name,
            "arch": entry.arch,
            "format": entry.format.value,
            "root_device": entry.root_device,
            "visibility": entry.visibility.value,
            "owner": entry.owner or "",
            "state": entry.state.value,
            "digest": entry.digest or "",
            "capabilities": list(entry.capabilities),
            "provenance": entry.provenance,
            "volume": entry.volume or "",
            "expires_at": entry.expires_at.isoformat() if entry.expires_at else "",
            "managed_by": entry.managed_by.value,
        },
        suggested_next_actions=[_LIST_TOOL],
    )


async def describe_image(
    pool: AsyncConnectionPool, ctx: RequestContext, image_id: str
) -> ToolResponse:
    """Return one catalog image visible to the caller, addressed by row id (ADR-0252).

    Visibility reuses the ``images.list`` predicate (public, or owned-private with viewer). A
    malformed id is a ``configuration_error``; a valid id with no visible row is ``not_found``
    (byte-identical whether absent or invisible — no existence/membership leak).
    """
    if _as_uuid(image_id) is None:
        return _invalid_uuid_error("image_id", image_id)
    with bind_context(principal=ctx.principal):
        params = {
            "id": image_id,
            "public": ImageVisibility.PUBLIC.value,
            "private": ImageVisibility.PRIVATE.value,
            "projects": projects_with_role(ctx, Role.VIEWER),
        }
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_DESCRIBE_SQL, params)
            row = await cur.fetchone()
    if row is None:
        return _not_found(image_id)
    return _describe_envelope(ImageCatalogEntry.model_validate(row))
```

Register the tool inside `register()` (after `images_list`):

```python
    @app.tool(
        name=_DESCRIBE_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def images_describe(
        image_id: Annotated[str, Field(description="The catalog image row id (UUID) to describe.")],
    ) -> ToolResponse:
        """Return full detail for one catalog image visible to the caller."""
        return await describe_image(pool, current_context(), image_id)
```

Add `images.describe` to `READ_TOOLS` in `tests/mcp/test_read_tools_annotated.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/mcp/catalog/test_images_describe.py tests/mcp/test_read_tools_annotated.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/catalog/images.py tests/mcp/catalog/test_images_describe.py tests/mcp/test_read_tools_annotated.py
git commit -m "feat(mcp): add images.describe per-image detail read tool

Refs #829

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `kdivectl images describe` CLI verb

**Files:**
- Modify: `src/kdive/cli/commands/reads.py:91-92` (add `images_describe`)
- Modify: `src/kdive/cli/commands/registry.py:133` (add the Verb)
- Test: `tests/cli/test_images_verbs.py` (add a describe case)

**Interfaces:**
- Consumes: `images.describe` tool (Task 4); `reads._record`.
- Produces: `reads.images_describe(args) -> int`; the `images describe` verb.

- [ ] **Step 1: Write the failing test** (add to `tests/cli/test_images_verbs.py`; reuses the
  file's existing `_install`/`_args`/`_FakeClient` helpers and `reads`/`REGISTRY` imports)

```python
def test_describe_calls_images_describe_read_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(
        monkeypatch, {"object_id": "img-1", "status": "registered", "data": {"name": "fedora"}}
    )
    code = asyncio.run(reads.images_describe(_args(image_id="img-1")))
    assert code == 0
    assert client.calls == [("images.describe", {"image_id": "img-1"})]


def test_describe_verb_registered_read_only() -> None:
    by_tool = {verb.tool: verb for verb in REGISTRY if verb.group == "images"}
    assert by_tool["images.describe"].read_only is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/cli/test_images_verbs.py -q`
Expected: FAIL (`images_describe` / the verb is undefined).

- [ ] **Step 3: Write minimal implementation**

`src/kdive/cli/commands/reads.py` (after `resources_describe`):

```python
async def images_describe(args: argparse.Namespace) -> int:
    return await _record("images.describe", args, {"image_id": args.image_id})
```

`src/kdive/cli/commands/registry.py` (next to the `images list` Verb at line 133):

```python
    Verb("images", "describe", reads.images_describe, "images.describe", ("image_id",)),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/cli/test_images_verbs.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/cli/commands/reads.py src/kdive/cli/commands/registry.py tests/cli/test_images_verbs.py
git commit -m "feat(cli): add kdivectl images describe verb

Refs #829

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Regenerate the tool-reference doc

**Files:**
- Modify (generated): `docs/guide/reference/images.md`, `docs/guide/reference/index.md`

- [ ] **Step 1: Regenerate**

Run: `just docs` (invokes `scripts/gen_tool_reference.py`). Inspect the diff — `images.md`
should now list `images.describe`.

- [ ] **Step 2: Verify the docs gate is clean**

Run: `just docs-check`
Expected: clean (no diff after regeneration).

- [ ] **Step 3: Commit**

```bash
git add docs/guide/reference/
git commit -m "docs(images): regenerate tool reference for images.describe

Refs #829

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Live verification (this host runs KVM/libvirt)

**Files:** none (verification only).

- [ ] **Step 1: Full local suite**

Run: `just lint && just type && just test`
Expected: all green.

- [ ] **Step 2: Live build + describe (live_vm)**

On this KVM/libvirt host, build a debug rootfs (e.g. the default Fedora 44 debug image) through
the real path, then `images.describe <id>` (or read the published row's `provenance`). Confirm
`package_versions` contains real installed versions for the kdump/drgn tooling (e.g.
`makedumpfile`, `drgn`). If `virt-inspector` is absent, confirm the build still succeeds and the
field is omitted (degrade path). Record the proof in the PR body.

- [ ] **Step 3: No commit** (verification only; capture results for the PR).

---

## Self-Review

**Spec coverage:**
- Read tool (UUID identity, RBAC, no-leak, projection, withheld path/object_key) → Task 4. ✓
- CLI parity → Task 5. ✓
- READ_TOOLS guard + generated doc → Tasks 4, 6. ✓
- Version seam (virt-inspector, XML parse, degrade) → Task 1. ✓
- Local + remote capture, per-plane filter set, additive provenance → Tasks 2, 3. ✓
- Exit criteria 1-6 → Tasks 4-6; 7-9 → Tasks 2-3; 10 (live) → Task 7. ✓
- `expires_at` isoformat → Task 4 `_describe_envelope`. ✓
- Defensive XML parse, empty-packages edge → Task 1 (DOCTYPE reject; empty map). ✓

**Type consistency:** `VersionInspectSeam = Callable[[Path], dict[str, str]]`,
`inspect_versions` field, `_capture_versions(...) -> dict[str, str]`, `describe_image(pool, ctx,
image_id) -> ToolResponse` used consistently across tasks.

**Placeholder scan:** every code-bearing step shows complete, runnable code (Task 5's CLI test
is concrete, mirroring the suite's `_install`/`_args` fake-session helpers; Task 1's seam shows a
single correct `virt-inspector` argv).
