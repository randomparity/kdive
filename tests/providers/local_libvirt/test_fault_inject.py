"""Tests for the local-libvirt fault-injection inspector (ADR-0825, ADR-0822)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.debug import fault_inject as mod
from kdive.providers.local_libvirt.debug.fault_inject import (
    LocalLibvirtFaultInjection,
    parse_helper_state,
)
from kdive.providers.local_libvirt.debug.live_introspect import _live_ssh_argv
from kdive.providers.ports.fault_injection import (
    FAULT_KNOB_PARAMETERS,
    READ_WORST_CASE_SECONDS,
    FaultKnobState,
)
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.security.secrets.secret_registry import SecretRegistry

_HANDLE = TransportHandleData(kind="ssh", host="127.0.0.1", port=2222).encode()
_HELPER = Path(__file__).parents[3] / "deploy/remote-libvirt-guest-helpers/kdive-faultinject"


def _knob(names: tuple[str, ...], **override: object) -> dict[str, object]:
    return {"present": True, **{name: 0 for name in names}, **override}


def _reply(**knobs: object) -> str:
    base: dict[str, object] = {
        "failslab": _knob(FAULT_KNOB_PARAMETERS["failslab"]),
        "fail_page_alloc": _knob(FAULT_KNOB_PARAMETERS["fail_page_alloc"]),
    }
    return json.dumps({"knobs": {**base, **knobs}})


def _inspector(exit_status: int, stdout: str | bytes, registry: SecretRegistry | None = None):
    raw = stdout.encode() if isinstance(stdout, str) else stdout
    return LocalLibvirtFaultInjection(
        secret_registry=registry or SecretRegistry(),
        run_helper=lambda handle, key: (exit_status, raw),
    )


def _error(inspector: LocalLibvirtFaultInjection) -> CategorizedError:
    with pytest.raises(CategorizedError) as caught:
        inspector.read_state(transport_handle=_HANDLE, key_path="/k")
    return caught.value


def test_read_state_returns_validated_integers_for_both_knobs() -> None:
    state = _inspector(0, _reply()).read_state(transport_handle=_HANDLE, key_path="/k")
    assert set(state) == {"failslab", "fail_page_alloc"}
    assert state["failslab"].present is True
    assert state["fail_page_alloc"].parameters["min-order"] == 0


def test_one_absent_knob_is_a_successful_read() -> None:
    state = parse_helper_state(_reply(fail_page_alloc={"present": False}))
    assert state["fail_page_alloc"] == FaultKnobState(present=False, parameters={})
    assert state["failslab"].present is True


def test_every_knob_absent_is_fault_injection_unsupported() -> None:
    reply = _reply(failslab={"present": False}, fail_page_alloc={"present": False})
    err = _error(_inspector(0, reply))
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.details["reason"] == "fault_injection_unsupported"
    assert "CONFIG_FAULT_INJECTION" in str(err)


@pytest.mark.parametrize(
    ("exit_status", "reason"),
    [
        (3, "debugfs_not_mounted"),
        (4, "fault_injection_unsupported"),
        (127, "helper_not_installed"),
    ],
)
def test_helper_exit_maps_to_a_configuration_error(exit_status: int, reason: str) -> None:
    err = _error(_inspector(exit_status, ""))
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.details["reason"] == reason


@pytest.mark.parametrize("exit_status", [1, 2, 5, 126])
def test_other_exit_is_an_infrastructure_failure_without_guest_text(exit_status: int) -> None:
    err = _error(_inspector(exit_status, "secret-guest-text"))
    assert err.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert err.details == {"reason": "helper_failed", "exit_status": exit_status}
    assert "secret-guest-text" not in str(err)


def test_reply_over_the_cap_is_refused_not_truncated() -> None:
    err = _error(_inspector(0, b"x" * 4097))
    assert err.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert err.details["reason"] == "helper_output_too_large"


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        "[]",
        json.dumps({"knobs": {"failslab": {"present": False}}}),
        json.dumps({"knobs": {}, "extra": 1}),
        _reply(failslab=_knob(FAULT_KNOB_PARAMETERS["failslab"], probability=True)),
        _reply(failslab={"present": True, "probability": 1}),
        _reply(failslab={**_knob(FAULT_KNOB_PARAMETERS["failslab"]), "task-filter": 1}),
        _reply(failslab={"present": False, "probability": 1}),
        _reply(failslab=_knob(FAULT_KNOB_PARAMETERS["failslab"], probability="1")),
    ],
)
def test_invalid_reply_shape_is_an_infrastructure_failure(reply: str) -> None:
    err = _error(_inspector(0, reply))
    assert err.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert err.details["reason"] == "helper_output_invalid"


def test_registered_secret_in_the_reply_is_redacted_before_parsing() -> None:
    registry = SecretRegistry()
    registry.register("0", scope=None)
    err = _error(_inspector(0, _reply(), registry))
    assert err.details["reason"] == "helper_output_invalid"


@pytest.mark.parametrize(
    ("probability", "times", "armed"),
    [(0, 1, False), (5, 0, False), (5, 1, True), (5, -1, True)],
)
def test_armed_requires_probability_and_nonzero_times(
    probability: int, times: int, armed: bool
) -> None:
    state = FaultKnobState(present=True, parameters={"probability": probability, "times": times})
    assert state.armed is armed
    assert FaultKnobState(present=False, parameters={}).armed is False


def test_knob_parameter_table_matches_the_helper_text() -> None:
    text = _HELPER.read_text(encoding="utf-8")
    for knob, names in FAULT_KNOB_PARAMETERS.items():
        match = re.search(rf'{knob}\) echo "([^"]+)"', text)
        assert match is not None, knob
        assert tuple(match.group(1).split()) == names


def test_argv_runs_the_helper_with_the_fixed_read_argument() -> None:
    argv = _live_ssh_argv(
        _HANDLE, SecretRegistry(), ["read"], "/k", program="/usr/local/sbin/kdive-faultinject"
    )
    assert argv[-3:] == ["--", "/usr/local/sbin/kdive-faultinject", "read"]
    assert mod._HELPER == "/usr/local/sbin/kdive-faultinject"


def test_argv_refuses_a_non_loopback_handle() -> None:
    handle = TransportHandleData(kind="ssh", host="10.0.0.5", port=22).encode()
    with pytest.raises(CategorizedError) as caught:
        _live_ssh_argv(handle, SecretRegistry(), ["read"], "/k", program=mod._HELPER)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_worst_case_call_time_matches_the_documented_figure() -> None:
    assert mod._SSH_TIMEOUT_S + mod._SSH_RETRY.deadline_s == READ_WORST_CASE_SECONDS
