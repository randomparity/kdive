"""Cover the synchronous rootfs catalog fetch for the local-libvirt provision lane (ADR-0228)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kdive.components.references import CatalogComponentRef
from kdive.config.core_settings import DATABASE_URL
from kdive.providers.local_libvirt.lifecycle import rootfs_catalog_fetch as mod


def test_fetch_threads_ref_arch_roots_and_cache_to_registered_rootfs(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    sentinel_conn = object()

    @contextmanager
    def _connect(url: str) -> Any:
        captured["url"] = url
        yield sentinel_conn

    def _fetch_registered(conn: Any, store_factory: Any, **kwargs: Any) -> Path:
        captured["conn"] = conn
        captured["store_factory"] = store_factory
        captured.update(kwargs)
        return Path("/srv/rootfs/img.qcow2")

    def _require(setting: Any) -> str:
        captured["setting"] = setting
        return "postgresql://db"

    monkeypatch.setattr(mod.psycopg, "connect", _connect)
    monkeypatch.setattr(mod.config, "require", _require)
    monkeypatch.setattr(mod, "fetch_registered_rootfs_sync", _fetch_registered)
    monkeypatch.setattr(mod, "object_store_from_env", "store-factory-sentinel")

    roots = [Path("/srv/rootfs")]
    fetch = mod.rootfs_catalog_fetch_from_env(roots)
    ref = CatalogComponentRef(kind="catalog", provider="local-libvirt", name="base")

    result = fetch(ref, "x86_64")

    assert result == Path("/srv/rootfs/img.qcow2")
    assert captured["setting"] is DATABASE_URL  # the DB url is resolved from the right setting
    assert captured["url"] == "postgresql://db"
    assert captured["conn"] is sentinel_conn
    assert captured["store_factory"] == "store-factory-sentinel"
    assert captured["allowed_roots"] == roots
    assert captured["provider"] == "local-libvirt"
    assert captured["name"] == "base"
    assert captured["arch"] == "x86_64"
    assert captured["cache_dir"] == mod._CACHE_DIR


def test_cache_dir_is_outside_the_rootfs_dir() -> None:
    # the s3-fetch cache must live outside allowed_roots so it is never a staged-path candidate
    assert mod._CACHE_DIR.name == "rootfs-cache"
    assert Path(mod.ROOTFS_DIR).parent / "rootfs-cache" == mod._CACHE_DIR
