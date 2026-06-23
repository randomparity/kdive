from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from kdive.components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    LocalComponentRef,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import _UploadRootfs
from kdive.providers.local_libvirt.lifecycle.materialize import (
    RootfsMaterializationContext,
    RootfsUploadContext,
    materialize_rootfs_base,
)
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from tests.providers.local_libvirt.fakes import FakeLibvirtConn


def test_materialize_local_rootfs_validates_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    root.mkdir()
    image = root / "base.qcow2"
    image.write_bytes(b"data")

    result = materialize_rootfs_base(
        LocalComponentRef(kind="local", path=str(image)),
        context=RootfsMaterializationContext(allowed_roots=[root]),
    )

    assert result == image.resolve()


def test_materialize_catalog_rootfs_uses_injected_fetch(tmp_path: Path) -> None:
    # The `catalog` path cuts over to the DB resolver + object fetch (ADR-0092): the context
    # supplies a fetch that returns the checksum-verified local cache path.
    cached = tmp_path / "abc.qcow2"
    cached.write_bytes(b"data")
    seen: list[tuple[CatalogComponentRef, str]] = []

    def _fetch(ref: CatalogComponentRef, arch: str) -> Path:
        seen.append((ref, arch))
        return cached

    ref = CatalogComponentRef(kind="catalog", provider="local-libvirt", name="base")
    result = materialize_rootfs_base(
        ref,
        context=RootfsMaterializationContext(
            allowed_roots=[tmp_path], arch="x86_64", catalog_fetch=_fetch
        ),
    )

    assert result == cached
    assert seen == [(ref, "x86_64")]


def test_materialize_catalog_rootfs_threads_arch(tmp_path: Path) -> None:
    # The provisioning profile's arch is threaded into the fetch so a same-name multi-arch image
    # resolves deterministically (ADR-0228).
    cached = tmp_path / "a.qcow2"
    cached.write_bytes(b"d")
    seen: list[str] = []

    def _fetch(_ref: CatalogComponentRef, arch: str) -> Path:
        seen.append(arch)
        return cached

    ref = CatalogComponentRef(kind="catalog", provider="local-libvirt", name="fed")
    materialize_rootfs_base(
        ref,
        context=RootfsMaterializationContext(
            allowed_roots=[tmp_path], arch="aarch64", catalog_fetch=_fetch
        ),
    )

    assert seen == ["aarch64"]


def test_materialize_local_rootfs_enforces_sha256(tmp_path: Path) -> None:
    # A LocalComponentRef sha256 must be threaded into the path validator: a digest that does not
    # match the file's contents is a configuration error, not a silently accepted path.
    root = tmp_path / "rootfs"
    root.mkdir()
    image = root / "base.qcow2"
    image.write_bytes(b"data")

    with pytest.raises(CategorizedError) as error:
        materialize_rootfs_base(
            LocalComponentRef(kind="local", path=str(image), sha256="sha256:" + "0" * 64),
            context=RootfsMaterializationContext(allowed_roots=[root]),
        )
    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "sha256" in str(error.value)


def test_materialize_catalog_rootfs_unwired_lane_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as error:
        materialize_rootfs_base(
            CatalogComponentRef(kind="catalog", provider="local-libvirt", name="base"),
            context=RootfsMaterializationContext(allowed_roots=[tmp_path]),
        )

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(error.value) == "catalog rootfs materialization is not wired for this lane"
    assert error.value.details == {"provider": "local-libvirt", "name": "base"}


def test_materialize_uploaded_rootfs_uses_system_keyed_path(tmp_path: Path) -> None:
    system_id = uuid4()

    result = materialize_rootfs_base(
        _UploadRootfs(kind="upload"),
        context=RootfsMaterializationContext(
            allowed_roots=[tmp_path],
            upload=RootfsUploadContext("local", system_id, tmp_path),
        ),
    )

    assert result == tmp_path / f"local-systems-{system_id}-rootfs.qcow2"


def test_materialize_uploaded_rootfs_requires_system_context(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as error:
        materialize_rootfs_base(
            _UploadRootfs(kind="upload"),
            context=RootfsMaterializationContext(allowed_roots=[tmp_path]),
        )

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(error.value) == "uploaded rootfs materialization requires upload context"


def test_provider_rejects_artifact_rootfs_before_materializer(tmp_path: Path) -> None:
    provisioner = LocalLibvirtProvisioning(
        connect=lambda: FakeLibvirtConn(), allowed_roots=[tmp_path]
    )

    with pytest.raises(CategorizedError) as error:
        provisioner.validate_rootfs_ref(ArtifactComponentRef(kind="artifact", artifact_id=uuid4()))

    assert error.value.category is ErrorCategory.MISSING_DEPENDENCY
