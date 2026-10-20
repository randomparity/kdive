"""Real catalog persistence/reconcile/admission proof for verified staging (ADR-0776)."""

from __future__ import annotations

import asyncio
from typing import Any, LiteralString
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.domain.errors import CategorizedError
from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile import images
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.remote_libvirt import stage_volume as wiring
from kdive.services.systems.root_provenance import resolve_root_provenance
from tests.reconcile_helpers import null_image_store

_DIGEST = "sha256:" + "a" * 64
_ROOT: dict[str, object] = {
    "schema": "root-spec-v1",
    "architecture": "x86_64",
    "root": "UUID=abc",
    "arguments": ["root=UUID=abc", "rootfstype=xfs"],
    "authority": "stage-inspection",
    "source": {"kind": "staged-image", "identity": _DIGEST},
}


def _doc(volume: str = "base.qcow2") -> InventoryDoc:
    return InventoryDoc.parse(
        {
            "schema_version": 2,
            "image": [
                {
                    "provider": "remote-libvirt",
                    "name": "base",
                    "arch": "x86_64",
                    "format": "qcow2",
                    "root_device": "/dev/vda",
                    "visibility": "public",
                    "source": {"kind": "staged", "volume": volume},
                }
            ],
        }
    )


def _seed(url: str) -> UUID:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "INSERT INTO image_catalog (provider,name,arch,format,root_device,visibility,state,"
            "volume,managed_by) VALUES ('remote-libvirt','base','x86_64','qcow2','/dev/vda',"
            "'public','registered','base.qcow2','config') RETURNING id"
        ).fetchone()
        assert row is not None
        return row[0]


def _check(url: str) -> dict[str, Any]:
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute("SELECT * FROM image_catalog WHERE name='base'").fetchone()
        assert row is not None
        return row


async def _reconcile(url: str, volume: str = "base.qcow2") -> None:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        await images.reconcile_images(conn, _doc(volume), null_image_store())


def test_persist_reconcile_and_admission_share_root_identity(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(migrated_url)
    monkeypatch.setattr(wiring.config, "require", lambda _setting: migrated_url)
    target = wiring._find_staged_row("remote-libvirt", "base", "x86_64")
    wiring._persist_root("remote-libvirt", "base", "x86_64", target, _DIGEST, _ROOT)
    asyncio.run(_reconcile(migrated_url))
    row = _check(migrated_url)
    assert row["digest"] == _DIGEST and row["provenance"]["root_spec"] == _ROOT

    async def resolve() -> None:
        profile = ProvisioningProfile.parse(
            {
                "schema_version": 1,
                "arch": "x86_64",
                "vcpu": 2,
                "memory_mb": 2048,
                "disk_gb": 20,
                "boot_method": "disk-image",
                "provider": {
                    "remote-libvirt": {
                        "base_image_source": {
                            "kind": "local",
                            "path": "/images/base.qcow2",
                            "sha256": _DIGEST,
                        }
                    }
                },
            }
        )
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            snapshot = await resolve_root_provenance(conn, profile, "project-a")
            assert snapshot is not None and snapshot.image_digest == _DIGEST

    asyncio.run(resolve())
    asyncio.run(_reconcile(migrated_url, "replacement.qcow2"))
    row = _check(migrated_url)
    assert row["digest"] is None and "root_spec" not in row["provenance"]
    asyncio.run(_reconcile(migrated_url))
    assert _check(migrated_url)["digest"] is None


@pytest.mark.parametrize("change", ["volume", "visibility", "managed_by", "state", "deleted"])
def test_persistence_refuses_changed_target(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _seed(migrated_url)
    monkeypatch.setattr(wiring.config, "require", lambda _setting: migrated_url)
    target = wiring._find_staged_row("remote-libvirt", "base", "x86_64")
    statements: dict[str, LiteralString] = {
        "volume": "UPDATE image_catalog SET volume='changed.qcow2' WHERE name='base'",
        "visibility": (
            "UPDATE image_catalog SET visibility='private',owner='project-a',"
            "expires_at=now()+interval '1 day' WHERE name='base'"
        ),
        "managed_by": "UPDATE image_catalog SET managed_by='runtime' WHERE name='base'",
        "state": (
            "UPDATE image_catalog SET state='pending',"
            "publication_attempt_id=gen_random_uuid() WHERE name='base'"
        ),
        "deleted": "DELETE FROM image_catalog WHERE name='base'",
    }
    with psycopg.connect(migrated_url) as conn:
        conn.execute(statements[change])
    with pytest.raises(CategorizedError, match="unverified"):
        wiring._persist_root("remote-libvirt", "base", "x86_64", target, _DIGEST, _ROOT)
    if change != "deleted":
        assert _check(migrated_url)["digest"] is None


def test_stale_reconcile_cannot_erase_new_stage_metadata(migrated_url: str) -> None:
    _seed(migrated_url)

    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            loaded = (await images._load_config_rows(conn))[("remote-libvirt", "base", "x86_64")]
            desired, _ = images._realize(
                _doc("replacement.qcow2").image[0], loaded, "not_s3", {}, False, None
            )
            await conn.execute(
                "UPDATE image_catalog SET digest=%s,provenance=%s WHERE name='base'",
                (_DIGEST, Jsonb({"root_spec": _ROOT})),
            )
            assert not await images._persist_runtime_fields(conn, loaded, desired)

    asyncio.run(run())
    assert _check(migrated_url)["digest"] == _DIGEST


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "build", "base": "base.qcow2"},
        {"kind": "s3", "object_key": "images/replacement.qcow2", "digest": "sha256:" + "b" * 64},
    ],
)
def test_changing_source_kind_cannot_retain_staged_authority(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, source: dict[str, str]
) -> None:
    _seed(migrated_url)
    monkeypatch.setattr(wiring.config, "require", lambda _setting: migrated_url)
    wiring._persist_root(
        "remote-libvirt",
        "base",
        "x86_64",
        wiring._find_staged_row("remote-libvirt", "base", "x86_64"),
        _DIGEST,
        _ROOT,
    )
    document = _doc().model_dump(mode="json")
    document["image"][0]["source"] = source

    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            await images.reconcile_images(conn, InventoryDoc.parse(document), null_image_store())

    asyncio.run(run())
    row = _check(migrated_url)
    assert row["digest"] != _DIGEST and "root_spec" not in row["provenance"]


@pytest.mark.parametrize("mode", ["absent", "unreachable", "no-digest", "present"])
def test_repoint_to_s3_requires_fresh_digest_and_head(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _seed(migrated_url)
    monkeypatch.setattr(wiring.config, "require", lambda _setting: migrated_url)
    wiring._persist_root(
        "remote-libvirt",
        "base",
        "x86_64",
        wiring._find_staged_row("remote-libvirt", "base", "x86_64"),
        _DIGEST,
        _ROOT,
    )
    document = _doc().model_dump(mode="json")
    source = {"kind": "s3", "object_key": "images/new.qcow2"}
    if mode != "no-digest":
        source["digest"] = "sha256:" + "b" * 64
    document["image"][0]["source"] = source
    store = null_image_store()
    heads: list[str] = []

    def head(key: str) -> bool:
        heads.append(key)
        if mode == "unreachable":
            from kdive.domain.errors import ErrorCategory

            raise CategorizedError("offline", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        return mode == "present"

    monkeypatch.setattr(store, "head_present", head)

    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            await images.reconcile_images(conn, InventoryDoc.parse(document), store)

    asyncio.run(run())
    row = _check(migrated_url)
    assert row["state"] == ("registered" if mode == "present" else "defined")
    assert row["object_key"] == ("images/new.qcow2" if mode == "present" else None)
    assert row["digest"] == ("sha256:" + "b" * 64 if mode == "present" else None)
    assert heads == ([] if mode == "no-digest" else ["images/new.qcow2"])
    assert "root_spec" not in row["provenance"]
