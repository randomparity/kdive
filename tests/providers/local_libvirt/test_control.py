"""LocalLibvirtControl provider tests — injected fake conn, no live host."""

from __future__ import annotations

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import PowerAction
from kdive.providers.local_libvirt.lifecycle.control import LocalLibvirtControl
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER
from tests.providers.local_libvirt.fakes import FakeDomain, FakeLibvirtConn, libvirt_error


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _control(
    domain: FakeDomain | None, *, clock: _Clock | None = None
) -> tuple[LocalLibvirtControl, FakeDomain | None]:
    lookup = {domain.domain_name: domain} if domain is not None else {}
    conn = FakeLibvirtConn(lookup=lookup)
    # The ADR-0576 truncate is observable through the same call log the fake domain keeps.
    target = domain

    def _prepare(name: str) -> None:
        if target is not None:
            target.calls.append("prepare")

    seams = {"sleep": clock.sleep, "clock": clock} if clock is not None else {}
    return LocalLibvirtControl(connect=lambda: conn, prepare_console=_prepare, **seams), domain


@pytest.mark.parametrize(
    ("action", "expected_calls"),
    [
        # ADR-0576: a power-on that starts a stopped domain truncates its console first, so
        # the new boot window never carries the prior boot's bytes.
        (PowerAction.ON, ["prepare", "create"]),
        (PowerAction.OFF, []),  # already SHUTOFF is an achieved target state
        (PowerAction.RESET, ["reset"]),
        (PowerAction.CYCLE, ["reboot"]),
        # #1254: resume must call virDomainResume, NOT reboot (which would destroy paused state).
        (PowerAction.RESUME, ["resume"]),
    ],
)
def test_power_maps_to_libvirt_call(action: PowerAction, expected_calls: list[str]) -> None:
    domain = FakeDomain(domain_name="kdive-x", system_id="x")
    control, domain = _control(domain)
    control.power("kdive-x", action)
    assert domain is not None and domain.calls == expected_calls


def test_power_on_running_domain_never_truncates() -> None:
    # A no-op power-on of an already-running guest must not wipe its live boot window
    # (ADR-0576): the OPERATION_INVALID from create stays swallowed and no truncate ran.
    domain = FakeDomain(
        domain_name="kdive-x",
        system_id="x",
        active=True,
        raise_on={"create": libvirt.VIR_ERR_OPERATION_INVALID},
    )
    control, domain = _control(domain)
    control.power("kdive-x", PowerAction.ON)  # no raise
    assert domain is not None and domain.calls == ["create"]


def test_power_off_running_guest_requests_clean_shutdown() -> None:
    domain = FakeDomain(domain_name="kdive-x", system_id="x", active=True)
    control, _ = _control(domain)
    control.power("kdive-x", PowerAction.OFF)
    assert domain.calls == ["shutdown"]


@pytest.mark.parametrize(("domain_type", "expected_s"), [("kvm", 60.0), ("qemu", 120.0)])
def test_power_off_timeout_uses_domain_accel_and_destroys(
    domain_type: str, expected_s: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LIBVIRT_TCG_DEADLINE_MULTIPLIER.name, "2.0")
    clock = _Clock()
    domain = FakeDomain(
        domain_name="kdive-x",
        system_id="x",
        active=True,
        honours_shutdown=False,
        xml_desc=f'<domain type="{domain_type}"/>',
    )
    control, _ = _control(domain, clock=clock)
    control.power("kdive-x", PowerAction.OFF)
    assert clock.now == expected_s
    assert domain.calls[0] == "shutdown"
    assert domain.calls[-1] == "destroy"


def test_power_off_refused_shutdown_destroys() -> None:
    domain = FakeDomain(
        domain_name="kdive-x",
        system_id="x",
        active=True,
        raise_on={"shutdown": libvirt.VIR_ERR_OPERATION_FAILED},
    )
    control, _ = _control(domain)
    control.power("kdive-x", PowerAction.OFF)
    assert domain.calls == ["shutdown", "destroy"]


def test_power_off_already_shutoff_ignores_malformed_xml() -> None:
    domain = FakeDomain(domain_name="kdive-x", system_id="x", xml_desc="<not XML")
    control, _ = _control(domain)
    control.power("kdive-x", PowerAction.OFF)
    assert domain.calls == []


@pytest.mark.parametrize("xml", ["<not XML", "<domain/>", '<domain type="unknown"/>'])
def test_power_off_active_invalid_xml_is_control_failure(xml: str) -> None:
    domain = FakeDomain(domain_name="kdive-x", system_id="x", active=True, xml_desc=xml)
    control, _ = _control(domain)
    with pytest.raises(CategorizedError) as exc:
        control.power("kdive-x", PowerAction.OFF)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert domain.calls == []


def test_power_off_unreadable_xml_is_control_failure() -> None:
    class UnreadableDomain(FakeDomain):
        def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
            raise libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)

    domain = UnreadableDomain(domain_name="kdive-x", system_id="x", active=True)
    control, _ = _control(domain)
    with pytest.raises(CategorizedError) as exc:
        control.power("kdive-x", PowerAction.OFF)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert domain.calls == []


def test_power_off_failed_destroy_is_control_failure() -> None:
    domain = FakeDomain(
        domain_name="kdive-x",
        system_id="x",
        active=True,
        run_state=libvirt.VIR_DOMAIN_PAUSED,
        raise_on={"destroy": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    control, _ = _control(domain)
    with pytest.raises(CategorizedError) as exc:
        control.power("kdive-x", PowerAction.OFF)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert domain.calls == ["destroy"]


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


_ALT = 56
_SYSRQ = 99


@pytest.mark.parametrize(
    ("trigger", "keycode"),
    [("t", 20), ("w", 17), ("m", 50), ("d", 32), ("p", 25), ("l", 38), ("q", 16)],
)
def test_diagnostic_sysrq_sends_alt_sysrq_keycombo(trigger: str, keycode: int) -> None:
    domain = FakeDomain(domain_name="kdive-x", system_id="x")
    control, domain = _control(domain)
    control.diagnostic_sysrq("kdive-x", trigger)
    assert domain is not None
    assert domain.sent_keys == [(libvirt.VIR_KEYCODE_SET_LINUX, [_ALT, _SYSRQ, keycode])]


def test_diagnostic_sysrq_absent_domain_is_control_failure() -> None:
    control, _ = _control(None)
    with pytest.raises(CategorizedError) as exc:
        control.diagnostic_sysrq("kdive-gone", "w")
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert str(exc.value) == "libvirt error looking up domain"
    assert exc.value.details == {"domain": "kdive-gone"}


def test_diagnostic_sysrq_libvirt_error_is_control_failure() -> None:
    domain = FakeDomain(
        domain_name="kdive-x",
        system_id="x",
        raise_on={"sendKey": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    control, _ = _control(domain)
    with pytest.raises(CategorizedError) as exc:
        control.diagnostic_sysrq("kdive-x", "w")
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert str(exc.value) == "libvirt error sending SysRq to domain"
    assert exc.value.details == {"domain": "kdive-x"}


def test_diagnostic_sysrq_unknown_trigger_is_configuration_error() -> None:
    domain = FakeDomain(domain_name="kdive-x", system_id="x")
    control, _ = _control(domain)
    with pytest.raises(CategorizedError) as exc:
        control.diagnostic_sysrq("kdive-x", "z")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


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
    # from_env wires the real name-keyed truncate; record instead of touching the host's
    # /var/lib/kdive/console in a unit test.
    prepared: list[str] = []
    monkeypatch.setattr(
        control_module, "prepare_console_for_domain", lambda name: prepared.append(name)
    )

    control = LocalLibvirtControl.from_env()
    assert prepared == []  # the seam is wired but not invoked until the start
    assert opened == []  # not connected yet
    control.power("kdive-x", PowerAction.ON)  # triggers the connect lambda

    assert opened == ["qemu+ssh://buildhost/system"]
    assert prepared == ["kdive-x"]  # the stopped domain was truncated before its create
