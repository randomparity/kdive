from __future__ import annotations

import pytest

from kdive.providers.system_authority.composition import _fixed_domain_exit


class _Domain:
    """Mirrors libvirt.virDomain: no free(); the handle is released when collected."""

    def __init__(self, active: int) -> None:
        self._active = active

    def isActive(self) -> int:  # noqa: N802
        return self._active


class _Connection:
    def __init__(self, domain: _Domain) -> None:
        self._domain = domain
        self.closed = False

    def lookupByName(self, name: str) -> _Domain:  # noqa: N802
        assert name == "kdive-system"
        return self._domain

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(("active", "exited"), [(1, False), (0, True)])
def test_domain_exit_probe_reads_an_existing_domain_and_closes_the_connection(
    active: int, exited: bool
) -> None:
    connection = _Connection(_Domain(active))

    probe = _fixed_domain_exit(lambda: connection, "kdive-system")

    assert probe.exited is exited
    assert connection.closed
