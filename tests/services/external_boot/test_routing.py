"""Worker authority-route admission stays aligned with its provider ownership."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.external_boot_authority.local_client import LocalAuthorityBinding
from kdive.providers.local_libvirt import composition as local_composition
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.external_boot import routing


def test_local_route_uses_the_worker_client_binding_not_runtime_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local runtime intentionally owns no authority sender."""
    runtime = local_composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.authority is None
    monkeypatch.setattr(
        routing,
        "local_authority_binding",
        lambda: LocalAuthorityBinding(
            "local-authority", Path("/run/authority.sock"), "ca", "cert", "key"
        ),
    )

    routing.require_worker_authority_route(
        ProviderBinding(ResourceKind.LOCAL_LIBVIRT, runtime), "local-authority"
    )


@pytest.mark.parametrize(
    "binding",
    [None, LocalAuthorityBinding("other", Path("/run/authority.sock"), "ca", "cert", "key")],
)
def test_local_route_refuses_missing_or_mismatched_worker_binding(
    monkeypatch: pytest.MonkeyPatch, binding: LocalAuthorityBinding | None
) -> None:
    runtime = local_composition.build_runtime(secret_registry=SecretRegistry())
    monkeypatch.setattr(routing, "local_authority_binding", lambda: binding)

    with pytest.raises(CategorizedError) as raised:
        routing.require_worker_authority_route(
            ProviderBinding(ResourceKind.LOCAL_LIBVIRT, runtime), "local-authority"
        )

    assert raised.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert raised.value.details == {"reason": "authority_route_mismatch"}


def test_local_route_propagates_a_partial_client_binding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = local_composition.build_runtime(secret_registry=SecretRegistry())
    partial = CategorizedError(
        "authority: incomplete-local-binding", category=ErrorCategory.CONFIGURATION_ERROR
    )

    def incomplete() -> LocalAuthorityBinding:
        raise partial

    monkeypatch.setattr(routing, "local_authority_binding", incomplete)

    with pytest.raises(CategorizedError) as raised:
        routing.require_worker_authority_route(
            ProviderBinding(ResourceKind.LOCAL_LIBVIRT, runtime), "local-authority"
        )

    assert raised.value is partial


def test_remote_route_still_requires_its_runtime_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = local_composition.build_runtime(secret_registry=SecretRegistry())
    binding = ProviderBinding(ResourceKind.REMOTE_LIBVIRT, runtime, "remote")

    with pytest.raises(CategorizedError) as raised:
        routing.require_worker_authority_route(binding, "remote-authority")

    assert raised.value.details == {"reason": "authority_route_missing"}
