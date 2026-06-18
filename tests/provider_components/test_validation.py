from __future__ import annotations

from uuid import UUID

import pytest

from kdive.components.references import ArtifactComponentRef, ComponentKind, LocalComponentRef
from kdive.components.validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_accepts_supported_component_source() -> None:
    caps = ComponentSourceCapabilities(
        provider="local-libvirt",
        accepted_component_sources={ComponentKind.ROOTFS: frozenset({"local"})},
    )

    reject_unsupported_component_source(
        caps,
        component_kind=ComponentKind.ROOTFS,
        ref=LocalComponentRef(kind="local", path="/var/lib/kdive/rootfs/base.qcow2"),
    )


def test_rejects_remote_provider_local_source() -> None:
    caps = ComponentSourceCapabilities(
        provider="remote-libvirt",
        accepted_component_sources={ComponentKind.ROOTFS: frozenset({"artifact", "catalog"})},
    )

    with pytest.raises(CategorizedError) as caught:
        reject_unsupported_component_source(
            caps,
            component_kind=ComponentKind.ROOTFS,
            ref=LocalComponentRef(kind="local", path="/var/lib/kdive/rootfs/base.qcow2"),
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "path" not in caught.value.details


def test_rejects_unimplemented_local_libvirt_kernel_artifact_source() -> None:
    caps = ComponentSourceCapabilities(
        provider="local-libvirt",
        accepted_component_sources={ComponentKind.KERNEL: frozenset({"local"})},
    )

    with pytest.raises(CategorizedError) as caught:
        reject_unsupported_component_source(
            caps,
            component_kind=ComponentKind.KERNEL,
            ref=ArtifactComponentRef(
                kind="artifact",
                artifact_id=UUID("00000000-0000-0000-0000-000000000000"),
            ),
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
