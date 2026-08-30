"""Shared authority-adapter contract and local-libvirt proofs (ADR-0584)."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.protocol import AuthorityMutationRequestV1
from kdive.providers.external_boot_authority.service import AuthorityMutationAdapter
from kdive.providers.local_libvirt.external_boot_authority import (
    LocalLibvirtAuthorityMutationAdapter,
)
from kdive.providers.shared.runtime_paths import domain_name_for

_A = "sha256:" + "a" * 64
_B = "sha256:" + "b" * 64


def authority_request() -> AuthorityMutationRequestV1:
    system_id, activation_id = uuid4(), uuid4()
    return AuthorityMutationRequestV1(
        authority_id=uuid4(),
        generation=1,
        system_id=system_id,
        activation_id=activation_id,
        run_id=uuid4(),
        plan_identity=_A,
        purpose="activate",
        provider_kind="local-libvirt",
        authority_instance="host-a",
        operation_identity="operation-a",
        operation_digest=_B,
        operation="activate",
        attempt_id=uuid4(),
        expected_source_identity=_A,
        intended_target_identity=_B,
        recovery_objects=(
            {"system_id": system_id, "activation_id": activation_id, "reference": "owned/a"},
        ),
    )


class FakeDomain:
    def __init__(self, state: str) -> None:
        self.state = state
        self.calls: list[str] = []
        self.fail_create = False

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        del flags
        if self.state == "raise-secret":
            raise RuntimeError("secret provider payload")
        return self.state

    def create(self) -> int:
        if self.fail_create:
            raise RuntimeError("secret provider payload")
        self.calls.append("activate")
        return 0

    def reset(self, flags: int) -> int:
        self.calls.append(f"recover:{flags}")
        return 0

    def destroy(self) -> int:
        self.calls.append("cleanup")
        return 0


class FakeConnection:
    def __init__(self, domain: FakeDomain) -> None:
        self.domain = domain
        self.lookups: list[str] = []
        self.closed = False

    def lookupByName(self, name: str) -> FakeDomain:  # noqa: N802
        self.lookups.append(name)
        return self.domain

    def close(self) -> int:
        self.closed = True
        return 0


type AdapterFactory = Callable[
    [FakeDomain], tuple[AuthorityMutationAdapter, Callable[[], list[str]]]
]


async def exercise_adapter_contract(factory: AdapterFactory) -> None:
    request = authority_request()
    for state, category in (
        (f"kdive-authority-state:{_A}", "source"),
        (f"kdive-authority-state:{_B}", "target"),
        ("kdive-authority-mixed:opaque", "mixed"),
        ("<domain/>", "conflict"),
        ("raise-secret", "unreadable"),
    ):
        adapter, _ = factory(FakeDomain(state))
        observation = await adapter.observe(request)
        assert observation.category == category
    for point, expected in (
        ("activate", "activate"),
        ("recover", "recover:0"),
        ("cleanup", "cleanup"),
    ):
        domain = FakeDomain(f"kdive-authority-state:{_B}")
        adapter, _ = factory(domain)
        assert (await adapter.commit(request, point)).category == "target"
        assert domain.calls == [expected]
    adapter, _ = factory(FakeDomain("<domain/>"))
    with pytest.raises(ValueError, match="unsupported external-boot commit point"):
        await adapter.commit(request, "hostile;command")
    domain = FakeDomain("raise-secret")
    adapter, _ = factory(domain)
    domain.fail_create = True
    with pytest.raises(CategorizedError) as raised:
        await adapter.commit(request, "activate")
    assert raised.value.category is ErrorCategory.CONTROL_FAILURE
    assert "secret provider payload" not in str(raised.value)
    assert request.recovery_objects[0].system_id == request.system_id


@pytest.mark.anyio
async def test_local_adapter_obeys_shared_contract_and_configured_uri() -> None:
    opened: list[str] = []

    def factory(
        domain: FakeDomain,
    ) -> tuple[LocalLibvirtAuthorityMutationAdapter, Callable[[], list[str]]]:
        def open_connection(uri: str) -> FakeConnection:
            opened.append(uri)
            return FakeConnection(domain)

        return LocalLibvirtAuthorityMutationAdapter(
            "qemu:///configured", open_connection=open_connection
        ), lambda: opened

    await exercise_adapter_contract(factory)
    assert opened and set(opened) == {"qemu:///configured"}


@pytest.mark.anyio
async def test_hostile_identities_are_opaque_and_never_provider_coordinates() -> None:
    request = authority_request().model_copy(
        update={
            "expected_source_identity": "qemu+ssh://evil/system;rm -rf x",
            "intended_target_identity": "/tmp/evil;command",
        }
    )
    domain = FakeDomain("<domain/>")
    connection = FakeConnection(domain)
    adapter = LocalLibvirtAuthorityMutationAdapter(
        "qemu:///configured", open_connection=lambda uri: connection
    )
    assert (await adapter.observe(request)).category == "conflict"
    assert connection.lookups == [domain_name_for(request.system_id)]
