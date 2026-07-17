"""The Snapshotter provider port + supports_snapshots capability (#1254, ADR-0378)."""

from __future__ import annotations

from kdive.providers.core.runtime import ProviderSupport
from kdive.providers.ports.lifecycle import Snapshotter


class _FakeSnapshotter:
    """A structural Snapshotter used to prove the Protocol is satisfiable by a fake."""

    def create(self, domain_name: str, name: str, *, include_memory: bool) -> None: ...
    def revert(self, domain_name: str, name: str, *, start_paused: bool) -> None: ...
    def delete(self, domain_name: str, name: str) -> None: ...
    def delete_all(self, domain_name: str) -> None: ...


def test_supports_snapshots_defaults_false() -> None:
    # Fail-closed: an unconfigured provider advertises no snapshot support.
    assert ProviderSupport().supports_snapshots is False


def test_provider_support_can_advertise_snapshots() -> None:
    assert ProviderSupport(supports_snapshots=True).supports_snapshots is True


def test_fake_satisfies_snapshotter_protocol() -> None:
    snap: Snapshotter = _FakeSnapshotter()
    # Exercising the four methods keeps the fake honest against the Protocol shape.
    snap.create("dom", "before-bug", include_memory=True)
    snap.revert("dom", "before-bug", start_paused=True)
    snap.delete("dom", "before-bug")
    snap.delete_all("dom")
