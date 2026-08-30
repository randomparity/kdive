"""Remote-libvirt authority adapter contract proofs (ADR-0584)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteLibvirtAuthorityMutationAdapter,
)
from kdive.providers.shared.runtime_paths import domain_name_for
from tests.providers.local_libvirt.test_external_boot_authority import (
    FakeConnection,
    FakeDomain,
    authority_request,
    exercise_adapter_contract,
)


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://configured/system",
        cert_refs=TlsCertRefs(
            client_cert_ref="fixture/cert",
            client_key_ref="fixture/key",  # pragma: allowlist secret
            ca_cert_ref="fixture/ca",
        ),
        concurrent_allocation_cap=1,
        storage_pool="default",
    )


@pytest.mark.anyio
async def test_remote_adapter_obeys_shared_contract_and_bound_config() -> None:
    seen: list[RemoteLibvirtConfig] = []

    def factory(domain: FakeDomain):  # noqa: ANN202 - test factory protocol is asserted by use
        @contextmanager
        def connection(config: RemoteLibvirtConfig):
            seen.append(config)
            yield FakeConnection(domain)

        return RemoteLibvirtAuthorityMutationAdapter(_config(), connection=connection), lambda: []

    await exercise_adapter_contract(factory)
    assert seen and all(config == _config() for config in seen)


@pytest.mark.anyio
async def test_hostile_identities_never_replace_remote_resource_config() -> None:
    request = authority_request().model_copy(
        update={
            "expected_source_identity": "qemu+ssh://evil/system;rm -rf x",
            "intended_target_identity": "/tmp/evil;command",
        }
    )
    domain = FakeDomain("<domain/>")
    connection_value = FakeConnection(domain)
    seen: list[RemoteLibvirtConfig] = []

    @contextmanager
    def connection(config: RemoteLibvirtConfig):
        seen.append(config)
        yield connection_value

    adapter = RemoteLibvirtAuthorityMutationAdapter(_config(), connection=connection)
    assert (await adapter.observe(request)).category == "conflict"
    assert seen == [_config()]
    assert connection_value.lookups == [domain_name_for(request.system_id)]
