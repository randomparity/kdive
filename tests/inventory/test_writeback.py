"""Writeback adapter seam for ``ops.export_systems_toml(persist=...)`` (#641, ADR-0199).

Three layers, each at its own boundary, none touching a real cluster:

* the skeleton guard — pure text check, shared marker with the serializer (drift test);
* the fake adapter — records the last write for the tool tests;
* the two real adapters + the factory — file writes to a tmp dir; the ConfigMap adapter's HTTP
  boundary is mocked (it is the external service). Secret material never reaches an error/detail.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory import serialize, writeback


def _run(coro: Awaitable[None]) -> None:
    asyncio.run(coro)


# ---- skeleton guard -------------------------------------------------------------------


def _defined_image() -> serialize.ImageRow:
    return serialize.ImageRow(
        provider="remote_libvirt",
        name="img-a",
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        visibility="public",
        capabilities=[],
        object_key=None,
        digest=None,
        volume=None,
        state="defined",
    )


def _remote_row() -> serialize.ResourceRow:
    return serialize.ResourceRow(
        name="host-a",
        cost_class="remote",
        pool="remote",
        host_uri="qemu+tls://host/system",
        vcpus=8,
        memory_mb=16384,
        concurrent_allocation_cap=2,
        seed=None,
    )


def _empty_snapshot(**rows: object) -> serialize.InventorySnapshot:
    base: dict[str, object] = {
        "images": (),
        "remote_libvirt": (),
        "local_libvirt": (),
        "fault_inject": (),
        "build_hosts": (),
        "cost_classes": (),
    }
    base.update(rows)
    return serialize.InventorySnapshot(**base)  # type: ignore[arg-type]


def test_assert_persistable_passes_clean_document() -> None:
    writeback.assert_persistable('name = "host-a"\nuri = "qemu+tls://h/system"\n')


def test_assert_persistable_rejects_a_placeholder() -> None:
    text = f'gdb_addr = "{serialize.REMOTE_PLACEHOLDER_PREFIX}gdb_addr"\n'
    with pytest.raises(CategorizedError) as exc:
        writeback.assert_persistable(text)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # the marker is named so the operator knows what to complete; no secret echoed.
    assert serialize.REMOTE_PLACEHOLDER_PREFIX in str(exc.value)


def test_guard_marker_matches_a_freshly_serialized_skeleton() -> None:
    # Drift guard: serialize a real skeleton (a remote host AND a defined image, the two
    # placeholder sites) and assert the guard's marker is present in the output, so changing
    # either placeholder prefix without the guard would fail this test.
    rendered = serialize.serialize_inventory(
        _empty_snapshot(images=(_defined_image(),), remote_libvirt=(_remote_row(),))
    )
    assert writeback.WRITEBACK_PLACEHOLDER_MARKER in rendered
    with pytest.raises(CategorizedError):
        writeback.assert_persistable(rendered)


def test_guard_does_not_flag_a_clean_export() -> None:
    # The export header explains REPLACE_ME_* in prose; a clean inventory (no remote host, no
    # defined image) must NOT trip the guard even though the header mentions the marker.
    rendered = serialize.serialize_inventory(
        _empty_snapshot(
            build_hosts=(
                serialize.BuildHostRow(
                    name="bh-local",
                    kind="local",
                    base_image_volume=None,
                    workspace_root="/var/lib/kdive/build",
                    max_concurrent=4,
                ),
            )
        )
    )
    assert "REPLACE_ME_" in rendered  # the header prose mentions it
    writeback.assert_persistable(rendered)  # but the guard does not fire


# ---- fake adapter ---------------------------------------------------------------------


def test_fake_records_the_last_write() -> None:
    fake = writeback.FakeWriteback()
    assert fake.written is None
    _run(fake.write("hello = 1\n"))
    assert fake.written == "hello = 1\n"
    assert fake.target_kind == "fake"


def test_fake_can_be_made_to_fail_and_records_nothing() -> None:
    boom = CategorizedError("nope", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
    fake = writeback.FakeWriteback(fail=boom)
    with pytest.raises(CategorizedError) as exc:
        _run(fake.write("x = 1\n"))
    assert exc.value is boom
    assert fake.written is None


# ---- mounted-file adapter -------------------------------------------------------------


def test_file_adapter_writes_atomically(tmp_path: Path) -> None:
    target = tmp_path / "systems.toml"
    adapter = writeback.MountedFileWriteback(target)
    assert adapter.target_kind == "file"
    _run(adapter.write('schema_version = 2\nname = "host-a"\n'))
    assert target.read_text() == 'schema_version = 2\nname = "host-a"\n'
    # no temp files left behind
    assert sorted(p.name for p in tmp_path.iterdir()) == ["systems.toml"]


def test_file_adapter_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "systems.toml"
    target.write_text("old = 1\n")
    _run(writeback.MountedFileWriteback(target).write("new = 2\n"))
    assert target.read_text() == "new = 2\n"


def test_file_adapter_missing_parent_is_configuration_error(tmp_path: Path) -> None:
    target = tmp_path / "absent-dir" / "systems.toml"
    with pytest.raises(CategorizedError) as exc:
        _run(writeback.MountedFileWriteback(target).write("x = 1\n"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(target) in str(exc.value)
    assert not target.exists()


# ---- ConfigMap adapter ----------------------------------------------------------------


def _configmap_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
) -> writeback.ConfigMapWriteback:
    transport = httpx.MockTransport(handler)
    return writeback.ConfigMapWriteback(
        namespace="kdive",
        name="kdive-systems",
        key="systems.toml",
        token="tok-abc",  # pragma: allowlist secret
        api_base="https://10.0.0.1:443",
        transport=transport,
    )


def test_configmap_patch_issues_the_expected_request() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"kind": "ConfigMap"})

    _run(_configmap_adapter(handler).write("schema_version = 2\n"))
    assert captured["method"] == "PATCH"
    # httpx canonicalizes the default :443 out of the URL; assert the path, not the port form.
    assert str(captured["url"]).endswith("/api/v1/namespaces/kdive/configmaps/kdive-systems")
    assert str(captured["url"]).startswith("https://10.0.0.1")
    assert captured["auth"] == "Bearer tok-abc"
    assert captured["content_type"] == "application/strategic-merge-patch+json"
    assert '"systems.toml"' in str(captured["body"])
    assert "schema_version = 2" in str(captured["body"])


def test_configmap_403_maps_to_configuration_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden: needs RBAC"})

    with pytest.raises(CategorizedError) as exc:
        _run(_configmap_adapter(handler).write("x = 1\n"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "kdive-systems" in str(exc.value)


def test_configmap_500_is_infrastructure_failure_without_body() -> None:
    leaky_body = "internal-cluster-detail-should-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=leaky_body)

    with pytest.raises(CategorizedError) as exc:
        _run(_configmap_adapter(handler).write("x = 1\n"))
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "500" in str(exc.value)
    # the response body (which can echo cluster internals) is never surfaced.
    assert leaky_body not in str(exc.value)
    assert leaky_body not in repr(exc.value.details)


def test_configmap_transport_error_is_infrastructure_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(CategorizedError) as exc:
        _run(_configmap_adapter(handler).write("x = 1\n"))
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    # the token is never echoed in an error.
    assert "tok-abc" not in str(exc.value)
    assert "tok-abc" not in repr(exc.value.details)


def test_configmap_from_in_cluster_without_mount_is_configuration_error(tmp_path: Path) -> None:
    # Point the service-account dir at an empty tmp dir → no token/namespace → fail closed.
    with pytest.raises(CategorizedError) as exc:
        writeback.ConfigMapWriteback.from_in_cluster(
            name="kdive-systems", key="systems.toml", service_account_dir=tmp_path
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# ---- factory --------------------------------------------------------------------------


def _load_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key in ("KDIVE_INVENTORY_WRITEBACK", "KDIVE_INVENTORY_WRITEBACK_CONFIGMAP"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config.load(dict(os.environ))


def test_factory_off_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_env(monkeypatch)
    assert writeback.resolve_writeback_target() is None
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="off")
    assert writeback.resolve_writeback_target() is None


def test_factory_file_returns_file_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "systems.toml"))
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="file")
    target = writeback.resolve_writeback_target()
    assert isinstance(target, writeback.MountedFileWriteback)
    assert target.target_kind == "file"


def test_factory_unknown_value_is_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="bogus")
    with pytest.raises(CategorizedError) as exc:
        writeback.resolve_writeback_target()
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "bogus" in str(exc.value)


def test_factory_configmap_outside_a_pod_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # configmap selected but no service-account mount → fail closed at resolve time.
    monkeypatch.setattr(writeback, "_SERVICE_ACCOUNT_DIR", tmp_path)
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="configmap")
    with pytest.raises(CategorizedError) as exc:
        writeback.resolve_writeback_target()
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
