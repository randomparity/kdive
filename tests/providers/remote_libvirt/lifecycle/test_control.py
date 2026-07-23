"""RemoteLibvirtControl tests — injected TLS opener + fake conn, no live host."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import PowerAction
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.lifecycle.control import RemoteLibvirtControl
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend
from tests.providers.remote_libvirt.fakes import FakeControlConn, FakeDomain

_SYSTEM_ID = UUID("00000000-0000-0000-0000-0000000000aa")


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
    )


def _control(domain: FakeDomain | None, tmp_path: Path) -> RemoteLibvirtControl:
    name = domain_name_for(_SYSTEM_ID)
    conn = FakeControlConn({name: domain} if domain is not None else {})
    return RemoteLibvirtControl(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: conn,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (PowerAction.ON, "create"),
        (PowerAction.OFF, "destroy"),
        (PowerAction.RESET, "reset"),
        (PowerAction.CYCLE, "reboot"),
    ],
)
def test_power_maps_to_libvirt_call(action: PowerAction, expected: str, tmp_path: Path) -> None:
    domain = FakeDomain(domain_name_for(_SYSTEM_ID))
    _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), action)
    assert domain.calls == [expected]


def test_power_on_already_running_swallowed(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name_for(_SYSTEM_ID),
        raise_on={"create": libvirt.VIR_ERR_OPERATION_INVALID},
    )
    _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), PowerAction.ON)  # no raise


@pytest.mark.parametrize(
    ("action", "call", "verb"),
    [
        (PowerAction.ON, "create", "starting"),
        (PowerAction.OFF, "destroy", "stopping"),
    ],
)
def test_idempotent_non_operation_invalid_error_is_control_failure(
    action: PowerAction, call: str, verb: str, tmp_path: Path
) -> None:
    # The idempotent on/off path swallows ONLY VIR_ERR_OPERATION_INVALID; any other libvirt
    # error (e.g. the domain genuinely failed to start/stop) must surface as a CONTROL_FAILURE
    # rather than being misreported as success.
    name = domain_name_for(_SYSTEM_ID)
    domain = FakeDomain(name, raise_on={call: libvirt.VIR_ERR_INTERNAL_ERROR})
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).power(name, action)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert str(exc.value) == f"libvirt error {action.value}-ing domain"
    assert exc.value.details == {"domain": name}
    assert domain.calls == [call]


def test_power_absent_domain_is_control_failure(tmp_path: Path) -> None:
    name = domain_name_for(_SYSTEM_ID)
    with pytest.raises(CategorizedError) as exc:
        _control(None, tmp_path).power(name, PowerAction.ON)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert str(exc.value) == "libvirt error looking up domain"
    assert exc.value.details == {"domain": name}


def test_connection_materializes_pki_under_the_configured_base_dir(tmp_path: Path) -> None:
    # The injected pki_base_dir must reach the TLS materialization so per-op cert material stays
    # inside the caller's sandbox; the composed connection URI references that pkipath.
    name = domain_name_for(_SYSTEM_ID)
    conn = FakeControlConn({name: FakeDomain(name)})
    seen_uris: list[str] = []

    def opener(uri: str) -> FakeControlConn:
        seen_uris.append(uri)
        return conn

    RemoteLibvirtControl(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=opener,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    ).power(name, PowerAction.ON)

    assert seen_uris
    assert str(tmp_path) in seen_uris[0]


def test_power_other_error_is_control_failure(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name_for(_SYSTEM_ID),
        raise_on={"reset": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), PowerAction.RESET)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE


def test_force_crash_injects_nmi(tmp_path: Path) -> None:
    domain = FakeDomain(domain_name_for(_SYSTEM_ID))
    _control(domain, tmp_path).force_crash(domain_name_for(_SYSTEM_ID))
    assert domain.calls == ["injectNMI"]


def test_force_crash_libvirt_error_is_control_failure(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name_for(_SYSTEM_ID),
        raise_on={"injectNMI": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).force_crash(domain_name_for(_SYSTEM_ID))
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE


class _FlagRecordingDomain(FakeDomain):
    """A FakeDomain that also records the flags argument passed to reset/reboot."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.flags: dict[str, int] = {}

    def reset(self, flags: int) -> int:
        self.flags["reset"] = flags
        return super().reset(flags)

    def reboot(self, flags: int) -> int:
        self.flags["reboot"] = flags
        return super().reboot(flags)


def test_power_reset_passes_zero_flags(tmp_path: Path) -> None:
    # libvirt's reset/reboot flags argument is reserved and must be 0; a nonzero value is
    # rejected by the binding.
    domain = _FlagRecordingDomain(domain_name_for(_SYSTEM_ID))
    _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), PowerAction.RESET)
    assert domain.flags == {"reset": 0}


def test_power_cycle_passes_zero_flags(tmp_path: Path) -> None:
    domain = _FlagRecordingDomain(domain_name_for(_SYSTEM_ID))
    _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), PowerAction.CYCLE)
    assert domain.flags == {"reboot": 0}


def test_power_failure_message_and_details_name_action_and_domain(tmp_path: Path) -> None:
    # A non-idempotent libvirt error surfaces a CONTROL_FAILURE whose message names the action
    # verb and whose details carry the domain name.
    name = domain_name_for(_SYSTEM_ID)
    domain = FakeDomain(name, raise_on={"reset": libvirt.VIR_ERR_INTERNAL_ERROR})
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).power(name, PowerAction.RESET)

    assert str(exc.value) == "libvirt error reset-ing domain"
    assert exc.value.details == {"domain": name}


def test_force_crash_failure_message_and_details_name_the_domain(tmp_path: Path) -> None:
    name = domain_name_for(_SYSTEM_ID)
    domain = FakeDomain(name, raise_on={"injectNMI": libvirt.VIR_ERR_INTERNAL_ERROR})
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).force_crash(name)

    assert str(exc.value) == "libvirt error injecting NMI into domain"
    assert exc.value.details == {"domain": name}


def test_diagnostic_sysrq_sends_alt_sysrq_chord(tmp_path: Path) -> None:
    # The transport-agnostic sendKey injection local uses (ADR-0285/0433): Alt+SysRq+<trigger> over
    # the remote connection. 'w' (show_blocked_tasks) → KEY_W keycode 17, with Alt(56)+SysRq(99).
    name = domain_name_for(_SYSTEM_ID)
    domain = FakeDomain(name)
    _control(domain, tmp_path).diagnostic_sysrq(name, "w")
    assert domain.calls == [f"sendKey:{libvirt.VIR_KEYCODE_SET_LINUX}:100:{[56, 99, 17]}:3:0"]


def test_diagnostic_sysrq_unknown_trigger_is_configuration_error(tmp_path: Path) -> None:
    # An unallowlisted trigger is a programming error (the tool validates the enum): it fails as a
    # CONFIGURATION_ERROR before any libvirt call, so no connection or sendKey is attempted.
    name = domain_name_for(_SYSTEM_ID)
    domain = FakeDomain(name)
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).diagnostic_sysrq(name, "c")  # destructive/absent from allowlist
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"domain": name, "trigger": "c"}
    assert domain.calls == []


def test_diagnostic_sysrq_libvirt_error_is_control_failure(tmp_path: Path) -> None:
    name = domain_name_for(_SYSTEM_ID)
    domain = FakeDomain(name, raise_on={"sendKey": libvirt.VIR_ERR_INTERNAL_ERROR})
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).diagnostic_sysrq(name, "w")
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert str(exc.value) == "libvirt error sending SysRq to domain"
    assert exc.value.details == {"domain": name}


def test_diagnostic_sysrq_absent_domain_is_control_failure(tmp_path: Path) -> None:
    name = domain_name_for(_SYSTEM_ID)
    with pytest.raises(CategorizedError) as exc:
        _control(None, tmp_path).diagnostic_sysrq(name, "w")
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert str(exc.value) == "libvirt error looking up domain"
