# Discoverable base-image volume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the operator-staged base-image volume token discoverable over MCP, and let `resources_describe` report whether each staged remote-libvirt image is present on the host's pool — so an MCP-only agent can pick a resource that can serve its image before allocating.

**Architecture:** Two read-surface changes plus one provider probe. (1) `images_list`/`fixtures_list` already read `image_catalog` rows carrying the `volume` column — surface it. (2) `resources_describe` for a `remote-libvirt` resource queries the caller-visible staged remote images, then calls an injected best-effort probe that opens one `qemu+tls://` connection (the shared `remote_connection` lifecycle) and runs the shared `lookup_volume_staged` helper (ADR-0150) once per volume, mapping the result into a `staged_base_images` list. The probe is bounded by a 5s timeout and degrades to `unreachable`/`unknown` without ever failing the describe.

**Tech Stack:** Python 3.13, `uv`, FastMCP, psycopg/psycopg_pool, libvirt-python, pytest. Guardrails via `just` (`just lint`, `just type`, `just test`, `just docs`).

**Spec:** `docs/design/discoverable-base-image-volume.md` · **ADR:** `docs/adr/0156-discoverable-base-image-volume.md`

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree (src + tests) — `just type`.
- Absolute imports only (no `..` relative paths).
- Every MCP tool returns a `ToolResponse`; `data` is `dict[str, JsonValue]` (nested lists/dicts allowed). A non-failure status must carry no `error_category`.
- Pick the most specific existing `ErrorCategory`; never invent strings. The probe never raises out — it returns a status map.
- The `image_catalog.provider` value for remote images is the literal `"remote-libvirt"`, equal to `ResourceKind.REMOTE_LIBVIRT.value` — use the enum value in code, not a bare string.
- No DDL: the `image_catalog.volume` column already exists (migration 0030).
- Run guardrails before each commit: `just lint && just type` and the focused test. Commit messages: conventional, ≤72-char subject, end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Doc prose rule: no "critical/crucial/essential/significant/comprehensive/robust/elegant/sprint".

---

### Task 1: Surface the `volume` token on `images_list` and `fixtures_list`

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/images.py` (`_row_envelope`, ~lines 37-50)
- Modify: `src/kdive/mcp/tools/catalog/fixtures.py` (`_public_rows`, ~lines 35-48)
- Test: `tests/mcp/catalog/test_images_list.py`, `tests/mcp/catalog/test_fixtures_list.py`

**Interfaces:**
- Consumes: `ImageCatalogEntry.volume: str | None` (`domain/models.py:417`) — already validated from the row.
- Produces: each `images_list` item `data` carries `"volume"`; each `fixtures_list` row carries `"volume"`. Empty string when the row has no staged volume.

- [ ] **Step 1: Write the failing test for `images_list`**

Add to `tests/mcp/catalog/test_images_list.py`. The existing `_insert` helper only inserts `local-libvirt` S3 rows (no volume); add a staged-row helper and a test.

```python
async def _insert_staged(pool: AsyncConnectionPool, *, name: str, volume: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, volume, visibility, owner, "
            " state, pending_since) "
            "VALUES ('remote-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(volume)s, "
            " 'public', NULL, 'registered', now())",
            {"name": name, "volume": volume},
        )


def _volume_of(resp: object, name: str) -> str:
    for item in getattr(resp, "items", []):
        if item.data["name"] == name:
            return str(item.data["volume"])
    raise AssertionError(f"{name} not in listing")


def test_list_carries_staged_volume_token(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_staged(pool, name="fedora-remote", volume="fedora-remote.qcow2")
            await _insert(pool, name="local-s3", visibility="public", owner=None)
            resp = await catalog_images.list_images(pool, _ctx())
        assert _volume_of(resp, "fedora-remote") == "fedora-remote.qcow2"
        assert _volume_of(resp, "local-s3") == ""  # no staged volume -> empty string

    asyncio.run(_run())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/mcp/catalog/test_images_list.py::test_list_carries_staged_volume_token -q`
Expected: FAIL with `KeyError: 'volume'`.

- [ ] **Step 3: Add `volume` to the `images_list` row envelope**

In `src/kdive/mcp/tools/catalog/images.py`, `_row_envelope`, add one line to the `data` dict:

```python
        data={
            "provider": entry.provider,
            "name": entry.name,
            "arch": entry.arch,
            "visibility": entry.visibility.value,
            "owner": entry.owner or "",
            "state": entry.state.value,
            "volume": entry.volume or "",
        },
```

- [ ] **Step 4: Run the `images_list` test to verify it passes**

Run: `uv run python -m pytest tests/mcp/catalog/test_images_list.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Write the failing test for `fixtures_list`**

Add to `tests/mcp/catalog/test_fixtures_list.py`. The `_insert_image` helper does not set `volume`; add a staged insert and assert the row carries it. (`_public_rows` returns dicts; the test reads `resp.data["fixtures"]`.)

```python
async def _insert_staged(conn: AsyncConnection, *, name: str, volume: str) -> None:
    await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, volume, capabilities, provenance, "
        " visibility, owner, state, managed_by) "
        "VALUES ('remote-libvirt', %s, 'x86_64', 'qcow2', '/dev/vda', %s, '{}', '{}', "
        " 'public', NULL, 'registered', 'config')",
        (name, volume),
    )


def test_fixtures_carry_staged_volume(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _insert_staged(conn, name="fedora-remote", volume="fedora-remote.qcow2")
            resp = await fixtures.list_fixtures_tool(pool)
        rows = resp.data["fixtures"]
        match = next(r for r in rows if r["name"] == "fedora-remote")
        assert match["volume"] == "fedora-remote.qcow2"

    asyncio.run(_run())
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `uv run python -m pytest tests/mcp/catalog/test_fixtures_list.py::test_fixtures_carry_staged_volume -q`
Expected: FAIL with `KeyError: 'volume'`.

- [ ] **Step 7: Add `volume` to `_public_rows`**

In `src/kdive/mcp/tools/catalog/fixtures.py`, `_public_rows`:

```python
        await cur.execute(
            "SELECT provider, name, arch, volume FROM image_catalog "
            "WHERE visibility = %s AND owner IS NULL "
            "ORDER BY provider, name, arch",
            (ImageVisibility.PUBLIC.value,),
        )
        rows = await cur.fetchall()
    return [
        {"provider": row["provider"], "name": row["name"], "arch": row["arch"],
         "volume": row["volume"] or ""}
        for row in rows
    ]
```

- [ ] **Step 8: Run the `fixtures_list` test to verify it passes**

Run: `uv run python -m pytest tests/mcp/catalog/test_fixtures_list.py -q`
Expected: PASS.

- [ ] **Step 9: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/catalog/images.py src/kdive/mcp/tools/catalog/fixtures.py \
        tests/mcp/catalog/test_images_list.py tests/mcp/catalog/test_fixtures_list.py
git commit -m "feat: surface staged base-image volume token on catalog reads"
```

---

### Task 2: Production staged-volume probe in the remote-libvirt package

**Files:**
- Create: `src/kdive/providers/remote_libvirt/staged_volumes.py`
- Test: `tests/providers/remote_libvirt/test_staged_volumes.py`

**Interfaces:**
- Consumes: `remote_config_from_inventory()` (`providers/remote_libvirt/config.py:221`), `remote_connection` + `open_libvirt_protocol` (`providers/remote_libvirt/transport.py`), `lookup_volume_staged` + `VolumeStaging` + `StorageConn` (`providers/remote_libvirt/lifecycle/storage.py`), `secret_backend_from_env` + `SecretRegistry`.
- Produces: `async def probe_staged_volumes(volumes: list[str], *, ..., timeout=_STAGED_PROBE_TIMEOUT_SECONDS) -> dict[str, str]` — maps each volume name to one of `staged`/`absent`/`pool_absent`/`unreachable`/`unknown`. Never raises. The seams (`config_factory`/`open_connection`/`secret_backend_factory`/`timeout`/`pki_base_dir`) are injectable for testing; production defaults wire the real ones. Used by Task 3 as the default probe (called positionally as `probe(volumes)`). Also exports `_STAGED_PROBE_TIMEOUT_SECONDS = 5.0`.

- [ ] **Step 1: Write the failing test**

Create `tests/providers/remote_libvirt/test_staged_volumes.py`. Model the fakes on `tests/diagnostics/test_base_image_staging.py`. The probe injects `config_factory`, `open_connection`, `secret_backend_factory` for testability (production defaults wire the real ones).

```python
"""Production staged-volume probe: maps libvirt pool lookups to per-volume status strings."""

from __future__ import annotations

import asyncio
from pathlib import Path

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt import staged_volumes
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs


def _config(pool: str = "kdive-pool") -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host/system",
        cert_refs=TlsCertRefs(client_cert_ref="c", client_key_ref="k", ca_cert_ref="a"),
        concurrent_allocation_cap=1,
        storage_pool=pool,
    )


class _Vol:
    pass


class _Pool:
    def __init__(self, staged: set[str]) -> None:
        self._staged = staged

    def storageVolLookupByName(self, name: str) -> _Vol:  # noqa: N802
        if name in self._staged:
            return _Vol()
        raise libvirt.libvirtError("no vol")


class _Conn:
    def __init__(self, staged: set[str], *, pool_exists: bool = True) -> None:
        self._staged = staged
        self._pool_exists = pool_exists

    def storagePoolLookupByName(self, name: str):  # noqa: N802, ANN201
        if not self._pool_exists:
            raise libvirt.libvirtError("no pool")
        return _Pool(self._staged)

    def close(self) -> None:
        pass


def _backend():
    from kdive.security.secrets.secret_registry import SecretRegistry
    from kdive.security.secrets.secrets import secret_backend_from_env

    return secret_backend_from_env(registry=SecretRegistry())


def _probe(
    volumes, *, conn=None, config_exc=None, transport_exc=False, block=False,
    timeout=5.0, tmp_path=None,
):
    import time

    def config_factory():
        if config_exc is not None:
            raise config_exc
        return _config()

    def open_connection(uri):
        if transport_exc:
            raise libvirt.libvirtError("connect refused")
        if block:
            # Exceed the injected timeout so wait_for fires first, but stay small: a to_thread
            # worker is not cancellable, so keep the orphaned sleep short to not stall teardown.
            time.sleep(1.0)
        return conn

    return asyncio.run(
        staged_volumes.probe_staged_volumes(
            volumes,
            config_factory=config_factory,
            open_connection=open_connection,
            secret_backend_factory=_backend,
            timeout=timeout,
            pki_base_dir=tmp_path,
        )
    )


def test_maps_staged_absent_and_pool_absent(tmp_path: Path) -> None:
    conn = _Conn(staged={"a.qcow2"})
    out = _probe(["a.qcow2", "b.qcow2"], conn=conn, tmp_path=tmp_path)
    assert out == {"a.qcow2": "staged", "b.qcow2": "absent"}


def test_pool_absent(tmp_path: Path) -> None:
    conn = _Conn(staged=set(), pool_exists=False)
    out = _probe(["a.qcow2"], conn=conn, tmp_path=tmp_path)
    assert out == {"a.qcow2": "pool_absent"}


def test_transport_failure_is_unreachable(tmp_path: Path) -> None:
    out = _probe(["a.qcow2"], transport_exc=True, tmp_path=tmp_path)
    assert out == {"a.qcow2": "unreachable"}


def test_config_error_is_unknown(tmp_path: Path) -> None:
    exc = CategorizedError("no instance", category=ErrorCategory.CONFIGURATION_ERROR)
    out = _probe(["a.qcow2"], config_exc=exc, tmp_path=tmp_path)
    assert out == {"a.qcow2": "unknown"}


def test_timeout_is_unreachable(tmp_path: Path) -> None:
    # A blocking connect with a tiny injected timeout must degrade to unreachable, fast.
    out = _probe(["a.qcow2", "b.qcow2"], block=True, timeout=0.05, tmp_path=tmp_path)
    assert out == {"a.qcow2": "unreachable", "b.qcow2": "unreachable"}


def test_empty_volumes_opens_nothing(tmp_path: Path) -> None:
    def config_factory():
        raise AssertionError("config must not be resolved for an empty volume list")

    out = asyncio.run(
        staged_volumes.probe_staged_volumes(
            [], config_factory=config_factory, open_connection=lambda uri: None,
            secret_backend_factory=_backend,
        )
    )
    assert out == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_staged_volumes.py -q`
Expected: FAIL with `ModuleNotFoundError`/`AttributeError` (module not created).

- [ ] **Step 3: Implement the probe**

Create `src/kdive/providers/remote_libvirt/staged_volumes.py`. Mirror `diagnostics/base_image_staging.py` structure: an injectable factory of seams with production defaults, blocking work in `asyncio.to_thread`, bounded by `asyncio.wait_for`.

```python
"""Server-vantage staged-volume probe for `resources.describe` (ADR-0156, #511).

Resolves the remote-libvirt connection config (URI, TLS refs, storage pool) internally,
opens one mutual-TLS `qemu+tls://` connection over the shared `remote_connection` lifecycle,
and runs the shared `lookup_volume_staged` helper (ADR-0150) once per requested volume. It is
best-effort: a transport failure / post-open libvirt error / timeout degrades every requested
volume to `unreachable`, and an unresolvable config degrades to `unknown` — it never raises.

The pool is `config.storage_pool` (the pool provisioning uses, ADR-0080 §5), never the
`Resource` row's advisory `pool` column.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import (
    RemoteLibvirtConfig,
    remote_config_from_inventory,
)
from kdive.providers.remote_libvirt.lifecycle.storage import (
    StorageConn,
    VolumeStaging,
    lookup_volume_staged,
)
from kdive.providers.remote_libvirt.transport import open_libvirt_protocol, remote_connection
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)

_STAGED_PROBE_TIMEOUT_SECONDS = 5.0

_STATUS_BY_STAGING: dict[VolumeStaging, str] = {
    VolumeStaging.STAGED: "staged",
    VolumeStaging.ABSENT: "absent",
    VolumeStaging.POOL_ABSENT: "pool_absent",
}


class _StorageProbeConn(StorageConn, Protocol):
    def close(self) -> None: ...


def _open_storage_connection(uri: str) -> _StorageProbeConn:
    return open_libvirt_protocol(uri)


def _default_secret_backend() -> SecretBackend:
    return secret_backend_from_env(registry=SecretRegistry())


async def probe_staged_volumes(
    volumes: list[str],
    *,
    config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_inventory,
    open_connection: Callable[[str], _StorageProbeConn] = _open_storage_connection,
    secret_backend_factory: Callable[[], SecretBackend] = _default_secret_backend,
    timeout: float = _STAGED_PROBE_TIMEOUT_SECONDS,
    pki_base_dir: Path | None = None,
) -> dict[str, str]:
    """Probe each volume's staged status on the remote host's pool; never raises.

    Returns a `{volume: status}` map where status is one of `staged`/`absent`/`pool_absent`
    (live pool verdicts), `unreachable` (host/RPC failure or timeout), or `unknown`
    (the remote config could not be resolved). An empty `volumes` opens no connection.

    `timeout` bounds the blocking libvirt work; it is injectable so a test can drive the
    timeout→unreachable path quickly without a real multi-second wait.
    """
    if not volumes:
        return {}
    try:
        config = config_factory()
    except CategorizedError:
        return dict.fromkeys(volumes, "unknown")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _probe_sync, config, volumes, open_connection, secret_backend_factory, pki_base_dir
            ),
            timeout,
        )
    except TimeoutError:
        _log.warning("staged-volume probe timed out after %.2fs", timeout)
        return dict.fromkeys(volumes, "unreachable")


def _probe_sync(
    config: RemoteLibvirtConfig,
    volumes: list[str],
    open_connection: Callable[[str], _StorageProbeConn],
    secret_backend_factory: Callable[[], SecretBackend],
    pki_base_dir: Path | None,
) -> dict[str, str]:
    try:
        with remote_connection(
            config,
            secret_backend_factory(),
            open_connection=open_connection,
            pki_base_dir=pki_base_dir,
        ) as conn:
            return {
                volume: _STATUS_BY_STAGING[lookup_volume_staged(conn, config.storage_pool, volume)]
                for volume in volumes
            }
    except CategorizedError as exc:
        if exc.category is ErrorCategory.TRANSPORT_FAILURE:
            return dict.fromkeys(volumes, "unreachable")
        return dict.fromkeys(volumes, "unknown")
    except libvirt.libvirtError:
        _log.warning("staged-volume probe storage lookup failed", exc_info=True)
        return dict.fromkeys(volumes, "unreachable")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_staged_volumes.py -q`
Expected: PASS (6 tests, including the fast `test_timeout_is_unreachable`). `RemoteLibvirtConfig`/`TlsCertRefs` fields are verified: `TlsCertRefs(client_cert_ref, client_key_ref, ca_cert_ref)` and `RemoteLibvirtConfig(uri, cert_refs, concurrent_allocation_cap, storage_pool=...)`.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/providers/remote_libvirt/staged_volumes.py \
        tests/providers/remote_libvirt/test_staged_volumes.py
git commit -m "feat: add server-vantage staged base-image volume probe"
```

---

### Task 3: Wire `resources_describe` to report `staged_base_images`

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/resources.py` (`describe_resource` ~100-118; `register` ~138-147)
- Test: `tests/mcp/catalog/test_resources_tools.py`

**Interfaces:**
- Consumes: `probe_staged_volumes` from Task 2 (`Callable[[list[str]], Awaitable[dict[str, str]]]`); `ResourceKind.REMOTE_LIBVIRT`; `projects_with_role(ctx, Role.VIEWER)`.
- Produces: `describe_resource(pool, ctx, resource_id, *, staged_probe=None)` adds a `staged_base_images` key (a list of `{name, volume, staged}` dicts) to the `data` of a `remote-libvirt` resource envelope; absent for other kinds. The injected `staged_probe` defaults to `probe_staged_volumes`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/mcp/catalog/test_resources_tools.py`. Insert a remote-libvirt resource row and staged catalog images directly, inject a fake probe (no libvirt). Add helpers near the top.

```python
from kdive.domain.models import ResourceKind  # add to imports if absent


async def _register_remote(pool: AsyncConnectionPool, *, host_uri: str = "qemu+tls://h/system") -> str:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO resources (kind, capabilities, pool, cost_class, status, host_uri) "
            "VALUES (%s, '{}', 'default', 'remote', 'available', %s) RETURNING id",
            (ResourceKind.REMOTE_LIBVIRT.value, host_uri),
        )
        row = await cur.fetchone()
    return str(row[0])


async def _insert_remote_image(
    pool: AsyncConnectionPool, *, name: str, volume: str, visibility: str = "public",
    owner: str | None = None,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, volume, visibility, owner, "
            " expires_at, state, pending_since) "
            "VALUES ('remote-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(volume)s, "
            " %(vis)s, %(owner)s, "
            " CASE WHEN %(vis)s = 'private' THEN now() + interval '1 hour' ELSE NULL END, "
            " 'registered', now())",
            {"name": name, "volume": volume, "vis": visibility, "owner": owner},
        )


def test_describe_remote_reports_staged_base_images(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool)
            await _insert_remote_image(pool, name="fedora", volume="fedora.qcow2")
            await _insert_remote_image(pool, name="dbg", volume="dbg.qcow2")

            async def fake_probe(volumes: list[str]) -> dict[str, str]:
                return {"fedora.qcow2": "staged", "dbg.qcow2": "absent"}

            return await catalog_resources_tools.describe_resource(
                pool, CTX, res_id, staged_probe=fake_probe
            )

    resp = asyncio.run(_run())
    assert resp.status == "available"
    staged = resp.data["staged_base_images"]
    assert {(r["name"], r["volume"], r["staged"]) for r in staged} == {
        ("dbg", "dbg.qcow2", "absent"),
        ("fedora", "fedora.qcow2", "staged"),
    }


def test_describe_remote_probe_failure_does_not_fail_describe(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool)
            await _insert_remote_image(pool, name="fedora", volume="fedora.qcow2")

            # The handler trusts the probe's returned map; a probe that degraded internally
            # returns 'unreachable' for every volume. The describe must still succeed.
            async def degraded(volumes: list[str]) -> dict[str, str]:
                return dict.fromkeys(volumes, "unreachable")

            return await catalog_resources_tools.describe_resource(
                pool, CTX, res_id, staged_probe=degraded
            )

    resp = asyncio.run(_run())
    assert resp.status == "available"
    assert resp.data["staged_base_images"][0]["staged"] == "unreachable"


def test_describe_no_staged_images_empty_list_probe_not_called(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool)

            async def fail(volumes: list[str]) -> dict[str, str]:
                raise AssertionError("probe must not be called when there are no staged images")

            return await catalog_resources_tools.describe_resource(
                pool, CTX, res_id, staged_probe=fail
            )

    resp = asyncio.run(_run())
    assert resp.data["staged_base_images"] == []


def test_describe_local_resource_has_no_staged_base_images(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)

            async def fail(volumes: list[str]) -> dict[str, str]:
                raise AssertionError("probe must not be called for a local resource")

            return await catalog_resources_tools.describe_resource(
                pool, CTX, res_id, staged_probe=fail
            )

    resp = asyncio.run(_run())
    assert "staged_base_images" not in resp.data


def test_describe_remote_excludes_other_projects_private_image(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool)
            await _insert_remote_image(
                pool, name="theirs", volume="theirs.qcow2", visibility="private", owner="proj-b"
            )

            async def probe(volumes: list[str]) -> dict[str, str]:
                return dict.fromkeys(volumes, "staged")

            return await catalog_resources_tools.describe_resource(
                pool, VIEWER_CTX, res_id, staged_probe=probe  # VIEWER_CTX is in 'proj', not proj-b
            )

    resp = asyncio.run(_run())
    assert resp.data["staged_base_images"] == []  # private remote image of another project hidden
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m pytest tests/mcp/catalog/test_resources_tools.py -k "staged or remote_excludes or local_resource_has_no" -q`
Expected: FAIL — `describe_resource()` has no `staged_probe` kwarg / no `staged_base_images` key.

- [ ] **Step 3: Implement the staged-image query + probe wiring**

In `src/kdive/mcp/tools/catalog/resources.py`:

Add imports near the top:

```python
from collections.abc import Awaitable, Callable

from kdive.providers.remote_libvirt.staged_volumes import probe_staged_volumes

StagedVolumeProbe = Callable[[list[str]], Awaitable[dict[str, str]]]

_STAGED_IMAGES_SQL = """
    SELECT name, volume
    FROM image_catalog
    WHERE provider = %(provider)s
      AND volume IS NOT NULL
      AND (visibility = %(public)s
           OR (visibility = %(private)s AND owner = ANY(%(projects)s)))
    ORDER BY name, arch
"""


async def _staged_remote_images(
    conn: AsyncConnection, ctx: RequestContext
) -> list[tuple[str, str]]:
    """Caller-visible staged remote-libvirt catalog images as `(name, volume)`."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _STAGED_IMAGES_SQL,
            {
                "provider": ResourceKind.REMOTE_LIBVIRT.value,
                "public": ImageVisibility.PUBLIC.value,
                "private": ImageVisibility.PRIVATE.value,
                "projects": projects_with_role(ctx, Role.VIEWER),
            },
        )
        return [(row["name"], row["volume"]) for row in await cur.fetchall()]
```

Add the `ImageVisibility` import: `from kdive.domain.models import ImageVisibility, Resource, ResourceKind`.

Rewrite `describe_resource` so the DB connection is released before the probe runs:

```python
async def describe_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    resource_id: str,
    *,
    staged_probe: StagedVolumeProbe | None = None,
) -> ToolResponse:
    """Return one resource's envelope with pool/cost_class/host_uri, or an error.

    For a remote-libvirt resource, also report `staged_base_images`: each caller-visible staged
    base-image volume and whether it is staged on the host's pool (ADR-0156). The live probe runs
    only after the DB connection is released, and degrades to a per-volume status — it never fails
    the describe.
    """
    try:
        uid = UUID(resource_id)
    except ValueError:
        return resource_config_error(resource_id)
    with bind_context(principal=ctx.principal):
        viewer_projects = tuple(projects_with_role(ctx, Role.VIEWER))
        async with pool.connection() as conn:
            resource = await RESOURCES.get(conn, uid)
            if resource is None or not resource_visible_to_projects(resource, viewer_projects):
                return resource_config_error(resource_id)
            staged_images: list[tuple[str, str]] = []
            if resource.kind is ResourceKind.REMOTE_LIBVIRT:
                staged_images = await _staged_remote_images(conn, ctx)
        envelope = resource_envelope(resource, next_actions=["allocations.request"])
        envelope.data["pool"] = resource.pool
        envelope.data["cost_class"] = resource.cost_class
        envelope.data["host_uri"] = resource.host_uri
        if resource.kind is ResourceKind.REMOTE_LIBVIRT:
            probe = staged_probe or probe_staged_volumes
            statuses = await probe([volume for _, volume in staged_images]) if staged_images else {}
            envelope.data["staged_base_images"] = [
                {"name": name, "volume": volume, "staged": statuses.get(volume, "unknown")}
                for name, volume in staged_images
            ]
        return envelope
```

(`projects_with_role` returns a list; `_staged_remote_images` passes it as the SQL `ANY` array param, matching `images.py`'s `_LIST_SQL`.)

- [ ] **Step 4: Run the resources tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/catalog/test_resources_tools.py -q`
Expected: PASS (existing + new). The `register()` wrapper needs no change — `resources_describe` calls `describe_resource(pool, current_context(), resource_id)` and the default `staged_probe=None` resolves to `probe_staged_volumes`.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/catalog/resources.py tests/mcp/catalog/test_resources_tools.py
git commit -m "feat: report per-resource staged base-image status on resources.describe"
```

---

### Task 4: Regenerate docs + full guardrail sweep

**Files:** possibly `docs/guide/reference/*` (generated tool reference) if any tool description changed (it should not — no docstring/param changes).

- [ ] **Step 1: Regenerate generated docs**

Run: `just docs`
Then `git status --short docs/guide/reference` — if anything changed, review and stage it. (Expected: no change, since this plan adds no params and no tool description text.)

- [ ] **Step 2: Full local gate**

Run: `just ci`
Expected: PASS. The DB/integration tests need Docker (testcontainers); if absent locally, run `just lint type docs-links docs-paths docs-check adr-status-check check-mermaid` and the non-DB subset, and note the DB-test limitation in the PR body. With Docker, `just ci` is the full gate.

- [ ] **Step 3: Commit any regenerated docs**

```bash
git add docs/guide/reference
git commit -m "docs: regenerate tool reference" || echo "no doc changes"
```

## Self-Review notes

- **Spec coverage:** Task 1 → catalog token (spec §1). Tasks 2-3 → per-resource probe (spec §2, including config.storage_pool source, unreachable/unknown split, 5s timeout, DB-conn-release-before-probe, empty-list short-circuit, RBAC). Task 4 → generated-doc guard.
- **Type consistency:** the probe signature `probe_staged_volumes(volumes: list[str]) -> dict[str,str]` (Task 2) matches `StagedVolumeProbe` and the `staged_probe` kwarg (Task 3). `_STATUS_BY_STAGING` covers the three `VolumeStaging` members; degraded paths add `unreachable`/`unknown`.
- **Pool source:** Task 2's `_probe_sync` uses `config.storage_pool`, never `resource.pool` — the spec's load-bearing decision.
