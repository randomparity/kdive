"""Pin the shared provider handle/record value types."""

from __future__ import annotations

from kdive.providers.ports.handles import OwnedInfra, SystemHandle, TransportHandle


def test_system_handle_is_a_str_newtype() -> None:
    h = SystemHandle("sys-1")
    assert h == "sys-1"
    assert SystemHandle.__supertype__ is str


def test_transport_handle_is_a_str_newtype() -> None:
    h = TransportHandle("tcp://host:1234")
    assert h == "tcp://host:1234"
    assert TransportHandle.__supertype__ is str


def test_owned_infra_declares_system_id_and_domain_name() -> None:
    assert set(OwnedInfra.__annotations__) == {"system_id", "domain_name"}
    infra: OwnedInfra = {"system_id": "s", "domain_name": "d"}
    assert infra["system_id"] == "s"
    assert infra["domain_name"] == "d"
