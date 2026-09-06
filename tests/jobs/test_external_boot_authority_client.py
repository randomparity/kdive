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
from kdive.providers.external_boot_authority.local_client import LocalAuthorityBinding
from kdive.providers.external_boot_authority.transport import _dispatch
from kdive.providers.remote_libvirt.config import RemoteAuthorityBinding
from tests.jobs.handlers.external_boot.support import marker_fields
from tests.providers.external_boot_authority.service_support import _mutation, _service


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
    with pytest.raises(CategorizedError, match="binding-mismatch"):
        await client.observe(_mutation(takeover).model_copy(update={"system_id": uuid4()}))
    assert deadlines == [123.0, 123.0]


def test_local_client_freezes_one_configured_route_at_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = LocalAuthorityBinding(
        "provider-1", Path("/run/test-authority.sock"), "ca", "cert", "key"
    )
    reads: list[str] = []
    selected: list[LocalAuthorityBinding] = []
    sender = cast(AuthorityRequestSender, object())

    def configuration() -> LocalAuthorityBinding:
        reads.append("configuration")
        return binding

    def local_sender(_secrets: Any, _borrow: Any, *, binding: LocalAuthorityBinding):
        selected.append(binding)
        return sender

    monkeypatch.setattr(clients, "local_authority_binding", configuration)
    monkeypatch.setattr(clients, "local_authority_sender_factory", local_sender)
    factory = clients.external_boot_client_factory(cast(Any, None), lambda: SecretStr("test"))
    assert reads == []
    marker = ExternalBootAuthorityMarkerV1.model_validate(marker_fields())
    runtime = ProviderBinding(ResourceKind.LOCAL_LIBVIRT, cast(Any, None), "resource-a")
    result = factory(runtime, marker, 123.0)
    assert reads == ["configuration"]
    assert selected == [binding]
    assert result.sender is sender
    assert result.deadline == 123.0
    with pytest.raises(CategorizedError, match="binding-mismatch"):
        factory(runtime, marker.model_copy(update={"authority_instance": "other"}), 123.0)
    assert selected == [binding]


def test_remote_route_is_selected_by_resolved_resource_not_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = {
        name: RemoteAuthorityBinding(name, "192.0.2.1", 9443, "ca", "cert", "key")
        for name in ("resource-a", "resource-b")
    }
    selected: list[RemoteAuthorityBinding] = []

    def sender(binding: RemoteAuthorityBinding) -> AuthorityRequestSender:
        selected.append(binding)
        return cast(AuthorityRequestSender, object())

    monkeypatch.setattr(clients, "authority_sender_factory", lambda *_: sender)
    monkeypatch.setattr(
        clients,
        "remote_config_for_resource",
        lambda name: SimpleNamespace(authority=bindings[name]),
    )
    factory = clients.external_boot_client_factory(cast(Any, None), lambda: SecretStr("test"))
    marker = ExternalBootAuthorityMarkerV1.model_validate(
        marker_fields(provider_kind="remote-libvirt", authority_instance="resource-a")
    )
    factory(
        ProviderBinding(ResourceKind.REMOTE_LIBVIRT, cast(Any, None), "resource-a"),
        marker,
        123.0,
    )
    with pytest.raises(CategorizedError, match="binding-mismatch"):
        factory(
            ProviderBinding(ResourceKind.REMOTE_LIBVIRT, cast(Any, None), "resource-b"),
            marker,
            123.0,
        )
    assert selected == [bindings["resource-a"]]
