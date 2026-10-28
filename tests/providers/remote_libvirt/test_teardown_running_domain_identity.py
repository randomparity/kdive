"""A running remote System's teardown validates the persistent definition and the live boot (#3377).

libvirt expands a running domain's XML (host-model CPU, ids, aliases, tty paths), so its preserved
digest never equals the one recorded at define time. The persistent document carries the preserved
and boot identities; the live document is held to the boot projection alone (ADR-0583).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import libvirt
import pytest

from kdive.providers.remote_libvirt.external_boot_operations import (
    ConcreteRemoteExternalBootOperations,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    boot_projection_identity,
    preserved_definition_identity,
)

_DATA = Path(__file__).parent / "data"
_SYSTEM = "00000000-0000-4000-8000-000000000001"
_INACTIVE = (_DATA / "running_domain_inactive.xml").read_text()
_LIVE = (_DATA / "running_domain_live.xml").read_text()


class _Domain:
    def __init__(self, live: str, inactive: str) -> None:
        self._documents = {0: live, libvirt.VIR_DOMAIN_XML_INACTIVE: inactive}

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - libvirt naming
        return self._documents[flags]


class _Volume:
    def path(self) -> str:
        return "/pool/overlay.qcow2"


class _Pool:
    def storageVolLookupByName(self, name: str) -> _Volume:  # noqa: N802 - libvirt naming
        return _Volume()


class _Connection:
    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802 - libvirt naming
        return _Pool()


def _state(inactive: str = _INACTIVE) -> Any:
    boot = boot_projection_identity(inactive)
    preserved = preserved_definition_identity(inactive)
    definition = SimpleNamespace(
        source_definition=preserved,
        source_boot=boot,
        target_definition=preserved,
        target_boot=boot,
    )
    return SimpleNamespace(
        binding=SimpleNamespace(system_id=_SYSTEM), recovery=SimpleNamespace(definition=definition)
    )


def _validate(domain: _Domain, state: Any) -> None:
    operations = ConcreteRemoteExternalBootOperations(
        cast(Any, object()), cast(Any, object()), "modules", lambda: 0.0, cast(Any, object())
    )
    operations._validate_teardown_domain(cast(Any, _Connection()), cast(Any, domain), state)


def test_the_fixtures_differ_the_way_a_running_domain_does() -> None:
    assert preserved_definition_identity(_LIVE) != preserved_definition_identity(_INACTIVE)
    assert boot_projection_identity(_LIVE) == boot_projection_identity(_INACTIVE)


def test_a_running_domain_matching_the_durable_definition_is_validated() -> None:
    _validate(_Domain(_LIVE, _INACTIVE), _state())


def test_a_stopped_domain_is_still_validated() -> None:
    _validate(_Domain(_INACTIVE, _INACTIVE), _state())


def test_a_persistent_definition_that_differs_is_refused() -> None:
    drifted = _INACTIVE.replace("<vcpu placement='static'>2", "<vcpu placement='static'>4")

    with pytest.raises(ValueError, match="domain identity differs from durable recovery"):
        _validate(_Domain(_LIVE, drifted), _state())


def test_a_live_boot_projection_that_differs_is_refused() -> None:
    retargeted = _LIVE.replace("rootfstype=btrfs", "rootfstype=xfs")

    with pytest.raises(ValueError, match="domain identity differs from durable recovery"):
        _validate(_Domain(retargeted, _INACTIVE), _state())


def test_a_persistent_boot_projection_that_differs_is_refused() -> None:
    retargeted = _INACTIVE.replace("rootfstype=btrfs", "rootfstype=xfs")

    with pytest.raises(ValueError, match="domain identity differs from durable recovery"):
        _validate(_Domain(_LIVE, retargeted), _state())
