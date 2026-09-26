from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

import kdive.providers.system_authority.composition as composition
from kdive.providers.local_libvirt.lifecycle.boot.readiness import ConsoleReadinessWindow
from kdive.providers.system_authority.composition import _fixed_domain_exit, _LocalReadiness


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


def test_local_readiness_forwards_authority_multiplier(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    class _Window:
        def close(self) -> None:
            pass

    def prepare(_system_id: object, *, multiplier: float) -> ConsoleReadinessWindow:
        seen.append(multiplier)
        return cast(ConsoleReadinessWindow, _Window())

    monkeypatch.setattr(composition, "prepare_console_readiness_window", prepare)
    readiness = _LocalReadiness(lambda: None, 7.0)
    readiness.prepare(Path(f"{uuid4()}.log"))
    readiness.close()

    assert seen == [7.0]
