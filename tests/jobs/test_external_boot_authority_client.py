"""A worker invocation retains one route, binding, and absolute deadline."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError
from kdive.jobs import external_boot_authority_client as clients
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.jobs.external_boot_authority_client import ExternalBootAuthorityClient
from kdive.jobs.models import ExternalBootAuthorityMarkerV1
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.external_boot_authority.transport import _dispatch
from kdive.providers.ports.authority import AuthorityCapability
from tests.jobs.handlers.external_boot.support import marker_fields
from tests.providers.external_boot_authority.service_support import _mutation, _service


@pytest.mark.parametrize("kind", [ResourceKind.LOCAL_LIBVIRT, ResourceKind.REMOTE_LIBVIRT])
def test_client_uses_only_the_bound_runtime_route(
    kind: ResourceKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = cast(AuthorityRequestSender, object())
    capability = AuthorityCapability(authority_instance="provider-1", sender=sender)
    if kind is ResourceKind.LOCAL_LIBVIRT:
        from kdive.providers.local_libvirt import composition

        monkeypatch.setattr(
            composition,
            "local_authority_binding",
            lambda: SimpleNamespace(authority_instance="provider-1"),
        )
        capability = composition.build_authority_capability(sender)
    binding = ProviderBinding(kind, cast(Any, SimpleNamespace(authority=capability)), "resource-a")
    marker = ExternalBootAuthorityMarkerV1.model_validate(marker_fields(provider_kind=kind.value))
    factory = clients.external_boot_client_factory()

    client = factory(binding, marker, 321.0)

    assert client.sender is sender
    assert client.deadline == 321.0
    with pytest.raises(CategorizedError, match="binding-mismatch"):
        factory(binding, marker.model_copy(update={"authority_instance": "other"}), 321.0)


@pytest.mark.anyio
async def test_one_client_keeps_deadline_through_ack_and_observation(tmp_path: Path) -> None:
    service, repository, _adapter, peer, takeover = _service(tmp_path)
    deadlines: list[float] = []

    async def authenticate(credential: SecretStr):
        assert credential.get_secret_value() == "test-incarnation"
        return peer

    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            deadlines.append(deadline)
            return await _dispatch(envelope, authenticate, service)

    values = takeover.model_dump()
    marker = ExternalBootAuthorityMarkerV1.model_validate(
        {key: values[key] for key in ExternalBootAuthorityMarkerV1.model_fields if key in values}
    )
    sender = AuthorityRequestSender(Backend, lambda: SecretStr("test-incarnation"))
    client = ExternalBootAuthorityClient(sender, marker, deadline=123.0)
    await client.acknowledge(takeover)
    repository.current = True
    await client.observe(_mutation(takeover))
    assert deadlines == [123.0, 123.0]
    mismatches = {
        "system_id": uuid4(),
        "activation_id": uuid4(),
        "run_id": uuid4(),
        "plan_identity": "sha256:" + "f" * 64,
        "purpose": "release",
        "provider_kind": "remote-libvirt",
        "authority_instance": "other",
    }
    for name, value in mismatches.items():
        assert getattr(marker, name) != value
        with pytest.raises(CategorizedError, match="binding-mismatch"):
            await client.observe(_mutation(takeover).model_copy(update={name: value}))
    assert deadlines == [123.0, 123.0]


@pytest.mark.parametrize(
    ("capability", "resource_name", "kind", "reason"),
    [
        (None, "resource-a", ResourceKind.REMOTE_LIBVIRT, "binding-mismatch"),
        (
            SimpleNamespace(authority_instance="provider-1", sender=None),
            "resource-a",
            ResourceKind.REMOTE_LIBVIRT,
            "binding-unavailable",
        ),
        (
            SimpleNamespace(authority_instance="other", sender=object()),
            "resource-a",
            ResourceKind.REMOTE_LIBVIRT,
            "binding-mismatch",
        ),
        (
            SimpleNamespace(authority_instance="provider-1", sender=object()),
            None,
            ResourceKind.REMOTE_LIBVIRT,
            "binding-unavailable",
        ),
        (
            SimpleNamespace(authority_instance="provider-1", sender=object()),
            "resource-a",
            ResourceKind.LOCAL_LIBVIRT,
            "binding-mismatch",
        ),
    ],
)
def test_unavailable_or_mismatched_runtime_route_is_refused(
    capability: object, resource_name: str | None, kind: ResourceKind, reason: str
) -> None:
    binding = ProviderBinding(kind, cast(Any, SimpleNamespace(authority=capability)), resource_name)
    marker = ExternalBootAuthorityMarkerV1.model_validate(
        marker_fields(provider_kind="remote-libvirt")
    )
    with pytest.raises(CategorizedError, match=reason):
        clients.external_boot_client_factory()(binding, marker, 123.0)
