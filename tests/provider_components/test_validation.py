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
    assert str(caught.value) == "provider does not accept this component source"
    assert caught.value.details == {
        "provider": "remote-libvirt",
        "component_kind": ComponentKind.ROOTFS,
        "source_kind": "local",
        "accepted_source_kinds": ["artifact", "catalog"],
    }


def test_rejects_when_component_kind_has_no_accepted_sources() -> None:
    """A component kind absent from the capability map accepts nothing and is rejected."""
    caps = ComponentSourceCapabilities(
        provider="remote-libvirt",
        accepted_component_sources={ComponentKind.ROOTFS: frozenset({"artifact"})},
    )

    with pytest.raises(CategorizedError) as caught:
        reject_unsupported_component_source(
            caps,
            component_kind=ComponentKind.KERNEL,
            ref=LocalComponentRef(kind="local", path="/var/lib/kdive/rootfs/base.qcow2"),
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details["accepted_source_kinds"] == []


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
