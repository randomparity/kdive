"""Unit tests for the constrained qemu-guest-agent exec primitive (issue #202, ADR-0078).

The primitive runs a worker-composed, allowlisted command in-guest via the
``guest-exec``/``guest-exec-status`` agent protocol over an injected ``agent_command``
callable (production: ``libvirt_qemu.qemuAgentCommand``); no real host is touched.
"""

from __future__ import annotations

import base64
import builtins
import itertools
import json
from collections.abc import Callable
from typing import Any

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.guest.agent import (
    BUILD_DETERMINISTIC_CONFIG_CODES,
    GuestAgentExec,
    qemu_agent_command,
)
from tests.providers.remote_libvirt.conftest import libvirt_error

_ALLOWED = frozenset({"/usr/bin/curl", "/usr/bin/kdive-install"})

# libvirt error codes that name a deterministic, non-retryable guest-agent condition
# (agent not configured / permission denied / unsupported); subcategorized to
# CONFIGURATION_ERROR at the raise site (ADR-0159, #531).
_DETERMINISTIC_CODES = (
    libvirt.VIR_ERR_ARGUMENT_UNSUPPORTED,
    libvirt.VIR_ERR_ACCESS_DENIED,
    libvirt.VIR_ERR_OPERATION_DENIED,
    libvirt.VIR_ERR_NO_SUPPORT,
    libvirt.VIR_ERR_OPERATION_UNSUPPORTED,
    libvirt.VIR_ERR_CONFIG_UNSUPPORTED,
)


def _float_clock() -> Callable[[], float]:
    """A monotonic stub that advances 2.0s per call without ever exhausting."""
    counter = itertools.count(0, 2)
    return lambda: float(next(counter))


class _FakeAgent:
    """Scripts ``guest-exec``→pid then ``guest-exec-status``→exit for one in-guest run."""

    def __init__(
        self,
        *,
        exitcode: int | None = 0,
        signal: int | None = None,
        out: bytes = b"",
        err: bytes = b"",
        status_sequence: list[bool] | None = None,
    ) -> None:
        self._exitcode = exitcode
        self._signal = signal
        self._out = out
        self._err = err
        # Each False is a not-yet-exited poll before the final exited=True.
        self._status_sequence = list(status_sequence or [True])
        self.commands: list[dict[str, Any]] = []
        self.timeouts: list[int] = []

    def __call__(self, domain: object, command: str, timeout: int, flags: int) -> str:
        parsed = json.loads(command)
        self.commands.append(parsed)
        self.timeouts.append(timeout)
        if parsed["execute"] == "guest-exec":
            return json.dumps({"return": {"pid": 4242}})
        if parsed["execute"] == "guest-exec-status":
            exited = self._status_sequence.pop(0) if self._status_sequence else True
            payload: dict[str, object] = {"exited": exited}
            if exited:
                # qemu-guest-agent reports exitcode on a normal exit OR signal on a kill.
                if self._signal is not None:
                    payload["signal"] = self._signal
                elif self._exitcode is not None:
                    payload["exitcode"] = self._exitcode
                if self._out:
                    payload["out-data"] = base64.b64encode(self._out).decode()
                if self._err:
                    payload["err-data"] = base64.b64encode(self._err).decode()
            return json.dumps({"return": payload})
        raise AssertionError(f"unexpected agent command {parsed!r}")


def _exec(agent: _FakeAgent) -> GuestAgentExec:
    return GuestAgentExec(
        agent_command=agent,
        allowed_programs=_ALLOWED,
        sleep=lambda _s: None,
        monotonic=_float_clock(),
    )


def test_run_returns_captured_stdout_and_exit_status() -> None:
    agent = _FakeAgent(exitcode=0, out=b"published-object-bytes")
    result = _exec(agent).run(object(), ["/usr/bin/curl", "-fsS", "https://store/obj"])
    assert result.exit_status == 0
    assert result.stdout == b"published-object-bytes"
    assert result.stderr == b""
    issued = [c["execute"] for c in agent.commands]
    assert issued == ["guest-exec", "guest-exec-status"]
    exec_args = agent.commands[0]["arguments"]
    assert exec_args["path"] == "/usr/bin/curl"
    assert exec_args["arg"] == ["-fsS", "https://store/obj"]
    assert exec_args["capture-output"] is True


def test_run_polls_until_the_command_exits() -> None:
    agent = _FakeAgent(out=b"done", status_sequence=[False, False, True])
    result = _exec(agent).run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert result.stdout == b"done"
    assert [c["execute"] for c in agent.commands].count("guest-exec-status") == 3


def test_run_rejects_a_non_allowlisted_program() -> None:
    agent = _FakeAgent()
    with pytest.raises(CategorizedError) as excinfo:
        _exec(agent).run(object(), ["/bin/sh", "-c", "curl https://store/obj"])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert agent.commands == []  # rejected before any agent round-trip


def test_run_rejects_an_empty_argv() -> None:
    agent = _FakeAgent()
    with pytest.raises(CategorizedError) as excinfo:
        _exec(agent).run(object(), [])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert agent.commands == []


def _exec_raising(exc: BaseException) -> GuestAgentExec:
    def boom(domain: object, command: str, timeout: int, flags: int) -> str:
        raise exc

    return GuestAgentExec(
        agent_command=boom,
        allowed_programs=_ALLOWED,
        sleep=lambda _s: None,
        monotonic=_float_clock(),
    )


def test_agent_unreachable_maps_to_transport_failure() -> None:
    # A bare libvirtError (no `.err` tuple) has no live error code: get_error_code() is None,
    # so it is not in the deterministic set and stays a retryable transport failure (#531).
    raised = libvirt.libvirtError("guest agent is not connected")

    with pytest.raises(CategorizedError) as excinfo:
        _exec_raising(raised).run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert excinfo.value.details["libvirt_error"] == "guest agent is not connected"
    assert excinfo.value.details["libvirt_error_code"] is None
    assert "domain" in excinfo.value.details


@pytest.mark.parametrize("code", _DETERMINISTIC_CODES)
def test_deterministic_libvirt_error_maps_to_configuration_error(code: int) -> None:
    # An agent that is not configured, denies the command, or cannot run it is a permanent
    # build-host condition; classify it CONFIGURATION_ERROR (retryable=false) so an agent does
    # not burn retry cycles on a failure that can never clear (#531, ADR-0159).
    with pytest.raises(CategorizedError) as excinfo:
        _exec_raising(libvirt_error(code)).run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["libvirt_error_code"] == code
    assert excinfo.value.details["libvirt_error"]  # the libvirt error string, non-empty
    assert "domain" in excinfo.value.details


@pytest.mark.parametrize(
    "code",
    [
        libvirt.VIR_ERR_AGENT_UNRESPONSIVE,
        libvirt.VIR_ERR_OPERATION_FAILED,
    ],
)
def test_transient_libvirt_error_stays_transport_failure(code: int) -> None:
    # A configured-but-not-currently-answering agent (VIR_ERR_AGENT_UNRESPONSIVE: mid-reconnect,
    # died, sync timeout) or an unrelated transient libvirt error keeps the retryable transport
    # classification — only the deterministic-config codes flip to CONFIGURATION_ERROR (#531).
    with pytest.raises(CategorizedError) as excinfo:
        _exec_raising(libvirt_error(code)).run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert excinfo.value.details["libvirt_error_code"] == code


def _exec_raising_with_codes(exc: BaseException, codes: frozenset[int]) -> GuestAgentExec:
    def boom(domain: object, command: str, timeout: int, flags: int) -> str:
        raise exc

    return GuestAgentExec(
        agent_command=boom,
        allowed_programs=_ALLOWED,
        deterministic_codes=codes,
        sleep=lambda _s: None,
        monotonic=_float_clock(),
    )


def test_build_deterministic_set_classifies_code_86_as_configuration_error() -> None:
    # The build transport runs only after the guest-ping readiness gate (ADR-0168), so a
    # post-readiness AGENT_UNRESPONSIVE is a deterministic dead agent for the build path:
    # constructed with BUILD_DETERMINISTIC_CONFIG_CODES it classifies code 86 non-retryable.
    exc = _exec_raising_with_codes(
        libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE), BUILD_DETERMINISTIC_CONFIG_CODES
    )
    with pytest.raises(CategorizedError) as excinfo:
        exc.run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["libvirt_error_code"] == libvirt.VIR_ERR_AGENT_UNRESPONSIVE


def test_build_deterministic_set_still_maps_base_codes_to_configuration_error() -> None:
    # Extending the set must not drop the ADR-0159 base codes.
    exc = _exec_raising_with_codes(
        libvirt_error(libvirt.VIR_ERR_ACCESS_DENIED), BUILD_DETERMINISTIC_CONFIG_CODES
    )
    with pytest.raises(CategorizedError) as excinfo:
        exc.run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_default_set_keeps_code_86_transport_failure() -> None:
    # The default classifier (install/retrieve/debug planes) is unchanged: code 86 stays
    # retryable transport_failure, preserving ADR-0159 for callers with no readiness gate.
    exc = _exec_raising(libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE))
    with pytest.raises(CategorizedError) as excinfo:
        exc.run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_qemu_agent_command_maps_missing_libvirt_qemu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def _import(
        name: str,
        globals_: dict[str, object] | None = None,
        locals_: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "libvirt_qemu":
            raise ModuleNotFoundError(name="libvirt_qemu")
        return original_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)

    with pytest.raises(CategorizedError) as excinfo:
        qemu_agent_command(object(), "{}", 1, 0)

    assert excinfo.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert excinfo.value.details == {"dependency": "libvirt_qemu"}


def test_qemu_agent_command_propagates_unrelated_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def _import(
        name: str,
        globals_: dict[str, object] | None = None,
        locals_: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "libvirt_qemu":
            raise ModuleNotFoundError(name="other_dependency")
        return original_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)

    with pytest.raises(ModuleNotFoundError) as excinfo:
        qemu_agent_command(object(), "{}", 1, 0)

    assert excinfo.value.name == "other_dependency"


def test_malformed_agent_response_maps_to_infrastructure_failure() -> None:
    def garbage(domain: object, command: str, timeout: int, flags: int) -> str:
        return "not json at all"

    exc = GuestAgentExec(
        agent_command=garbage,
        allowed_programs=_ALLOWED,
        sleep=lambda _s: None,
        monotonic=_float_clock(),
    )
    with pytest.raises(CategorizedError) as excinfo:
        exc.run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_agent_calls_use_a_bounded_positive_timeout() -> None:
    # A blocking (-2) timeout would let a disconnected agent wedge the worker; each
    # call must carry a positive bound so the seam's deadline governs total time.
    agent = _FakeAgent(out=b"ok")
    _exec(agent).run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert agent.timeouts  # at least one round-trip happened
    assert all(timeout > 0 for timeout in agent.timeouts)


def test_signal_killed_command_is_not_reported_as_success() -> None:
    # guest-exec-status returns `signal` (no exitcode) when the process is killed
    # (OOM, timeout-kill, SIGSEGV); defaulting a missing exitcode to 0 would read
    # a killed install as success.
    agent = _FakeAgent(exitcode=None, signal=9, out=b"partial")
    result = _exec(agent).run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert result.exit_status != 0
    assert result.exit_status == 128 + 9


def test_exited_with_neither_exitcode_nor_signal_is_not_success() -> None:
    # A `guest-exec-status` reply that reports `exited: true` but carries neither
    # `exitcode` nor `signal` is abnormal — the agent normally reports exactly one for
    # a reaped process. Defaulting it to 0 masks a command of unknown outcome as a pass
    # (issue #517), so it must raise INFRASTRUCTURE_FAILURE rather than return success.
    agent = _FakeAgent(exitcode=None, signal=None, out=b"partial")
    with pytest.raises(CategorizedError) as excinfo:
        _exec(agent).run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_run_times_out_when_the_command_never_exits() -> None:
    agent = _FakeAgent(status_sequence=[False] * 50)
    exc = GuestAgentExec(
        agent_command=agent,
        allowed_programs=_ALLOWED,
        timeout_s=6.0,
        sleep=lambda _s: None,
        monotonic=iter([0.0, 2.0, 4.0, 6.0, 8.0]).__next__,
    )
    with pytest.raises(CategorizedError) as excinfo:
        exc.run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
