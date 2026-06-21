"""LocalLibvirtControl provider tests — injected fake conn, no live host."""

from __future__ import annotations

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import PowerAction
from kdive.providers.local_libvirt.lifecycle.control import LocalLibvirtControl
from tests.providers.local_libvirt.fakes import FakeDomain, FakeLibvirtConn


def _control(domain: FakeDomain | None) -> tuple[LocalLibvirtControl, FakeDomain | None]:
    lookup = {domain.domain_name: domain} if domain is not None else {}
    conn = FakeLibvirtConn(lookup=lookup)
    return LocalLibvirtControl(connect=lambda: conn), domain


@pytest.mark.parametrize(
    ("action", "expected_call"),
    [
        (PowerAction.ON, "create"),
        (PowerAction.OFF, "destroy"),
        (PowerAction.RESET, "reset"),
        (PowerAction.CYCLE, "reboot"),
    ],
)
def test_power_maps_to_libvirt_call(action: PowerAction, expected_call: str) -> None:
    domain = FakeDomain(domain_name="kdive-x", system_id="x")
    control, domain = _control(domain)
    control.power("kdive-x", action)
    assert domain is not None and domain.calls == [expected_call]


def test_power_on_already_running_swallowed() -> None:
    domain = FakeDomain(
        domain_name="kdive-x",
        system_id="x",
        raise_on={"create": libvirt.VIR_ERR_OPERATION_INVALID},
    )
    control, _ = _control(domain)
    control.power("kdive-x", PowerAction.ON)  # no raise


def test_power_off_not_running_swallowed() -> None:
    domain = FakeDomain(
        domain_name="kdive-x",
        system_id="x",
        raise_on={"destroy": libvirt.VIR_ERR_OPERATION_INVALID},
    )
    control, _ = _control(domain)
    control.power("kdive-x", PowerAction.OFF)  # no raise


def test_power_absent_domain_is_control_failure() -> None:
    control, _ = _control(None)
    with pytest.raises(CategorizedError) as exc:
        control.power("kdive-gone", PowerAction.ON)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    # The lookup-failure error names the looked-up domain and the lookup verb.
    assert str(exc.value) == "libvirt error looking up domain"
    assert exc.value.details == {"domain": "kdive-gone"}


def test_power_other_libvirt_error_is_control_failure() -> None:
    domain = FakeDomain(
        domain_name="kdive-x",
        system_id="x",
        raise_on={"reset": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    control, _ = _control(domain)
    with pytest.raises(CategorizedError) as exc:
        control.power("kdive-x", PowerAction.RESET)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    # The apply-power failure verb is derived from the action value and the domain is named.
    assert str(exc.value) == "libvirt error reset-ing domain"
    assert exc.value.details == {"domain": "kdive-x"}


def test_force_crash_injects_nmi() -> None:
    domain = FakeDomain(domain_name="kdive-x", system_id="x")
    control, domain = _control(domain)
    control.force_crash("kdive-x")
    assert domain is not None and domain.calls == ["injectNMI"]


def test_force_crash_absent_domain_is_control_failure() -> None:
    control, _ = _control(None)
    with pytest.raises(CategorizedError) as exc:
        control.force_crash("kdive-gone")
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert str(exc.value) == "libvirt error looking up domain"
    assert exc.value.details == {"domain": "kdive-gone"}


def test_force_crash_libvirt_error_is_control_failure() -> None:
    domain = FakeDomain(
        domain_name="kdive-x",
        system_id="x",
        raise_on={"injectNMI": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    control, _ = _control(domain)
    with pytest.raises(CategorizedError) as exc:
        control.force_crash("kdive-x")
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    # The NMI-injection failure names the inject-NMI verb and the domain.
    assert str(exc.value) == "libvirt error injecting NMI into domain"
    assert exc.value.details == {"domain": "kdive-x"}


def test_from_env_connect_opens_configured_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    # from_env reads KDIVE_LIBVIRT_URI and wires a connect that opens exactly that URI; it does
    # not connect eagerly (the lambda is only invoked here).
    import kdive.providers.local_libvirt.lifecycle.control as control_module

    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu+ssh://buildhost/system")
    opened: list[str] = []

    domain = FakeDomain(domain_name="kdive-x", system_id="x")

    def _fake_open(uri: str) -> FakeLibvirtConn:
        opened.append(uri)
        return FakeLibvirtConn(lookup={"kdive-x": domain})

    monkeypatch.setattr(control_module.libvirt, "open", _fake_open)

    control = LocalLibvirtControl.from_env()
    assert opened == []  # not connected yet
    control.power("kdive-x", PowerAction.ON)  # triggers the connect lambda

    assert opened == ["qemu+ssh://buildhost/system"]
