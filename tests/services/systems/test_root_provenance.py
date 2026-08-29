"""System root-provenance snapshot resolution tests (ADR-0583, #2106)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.services.systems.root_provenance import resolve_root_provenance

_DIGEST = "sha256:" + "a" * 64


def _profile(*, checksum: str | None = _DIGEST, arch: str = "x86_64") -> ProvisioningProfile:
    source: dict[str, object] = {"kind": "local", "path": "/images/base.qcow2"}
    if checksum is not None:
        source["sha256"] = checksum
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": arch,
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "disk-image",
            "provider": {"remote-libvirt": {"base_image_source": source}},
        }
    )


def _row(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": uuid4(),
        "arch": "x86_64",
        "digest": _DIGEST,
        "provenance": {
            "root_spec": {
                "schema": "root-spec-v1",
                "architecture": "x86_64",
                "root": "UUID=abc",
                "arguments": ["root=UUID=abc", "rootfstype=xfs"],
                "authority": "stage-inspection",
                "source": {"kind": "staged-image", "identity": _DIGEST},
            }
        },
    }
    value.update(changes)
    return value


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, *_args: object) -> None:
        return None

    async def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return _Cursor(self.rows)


def _conn(rows: list[dict[str, object]]) -> AsyncConnection:
    return cast("AsyncConnection", _Connection(rows))


def test_resolves_verified_digest_bound_root() -> None:
    result = asyncio.run(resolve_root_provenance(_conn([_row()]), _profile(), "project-a"))
    assert result is not None
    assert result.project == "project-a"
    assert result.root_spec.root == "UUID=abc"


def test_missing_checksum_or_catalog_row_is_disk_grub_only() -> None:
    assert asyncio.run(resolve_root_provenance(_conn([]), _profile(), "p")) is None
    result = asyncio.run(resolve_root_provenance(_conn([_row()]), _profile(checksum=None), "p"))
    assert result is None


def test_duplicate_digest_authorities_fail_closed() -> None:
    with pytest.raises(CategorizedError) as exc:
        asyncio.run(resolve_root_provenance(_conn([_row(), _row()]), _profile(), "p"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["reason"] == "ambiguous_root_provenance"


@pytest.mark.parametrize(
    "row",
    [
        _row(arch="ppc64le"),
        _row(
            provenance={
                "root_spec": {
                    "schema": "root-spec-v1",
                    "architecture": "x86_64",
                    "root": "UUID=abc",
                    "arguments": ["root=UUID=abc", "rootfstype=xfs"],
                    "authority": "stage-inspection",
                    "source": {"kind": "staged-image", "identity": "sha256:" + "b" * 64},
                }
            }
        ),
    ],
)
def test_incompatible_or_stale_authority_fails_closed(row: dict[str, object]) -> None:
    with pytest.raises(CategorizedError) as exc:
        asyncio.run(resolve_root_provenance(_conn([row]), _profile(), "p"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
