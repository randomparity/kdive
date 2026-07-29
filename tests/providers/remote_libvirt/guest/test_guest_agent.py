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

from kdive.domain.errors import CategorizedError, ErrorCategory, retryable_category
from kdive.providers.remote_libvirt.guest.agent import GuestAgentExec, qemu_agent_command
from tests.providers.remote_libvirt.conftest import libvirt_error


class _StubDomain:
    """A minimal libvirt-domain stand-in: the guest-exec seam only reads ``name()``."""

    def name(self) -> str:
        return "build-vm"


_DOMAIN = _StubDomain()

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
        self.domains: list[object] = []
        self.flags: list[int] = []

    def __call__(self, domain: object, command: str, timeout: int, flags: int) -> str:
        parsed = json.loads(command)
        self.commands.append(parsed)
        self.timeouts.append(timeout)
        self.domains.append(domain)
        self.flags.append(flags)
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


def test_run_passes_input_data_as_base64_stdin() -> None:
    agent = _FakeAgent(exitcode=0, out=b"ok")
    _exec(agent).run(
        _DOMAIN,
        ["/usr/bin/kdive-install", "run-script", "30"],
        input_data="print(1)\n",
    )
    exec_args = agent.commands[0]["arguments"]
    assert exec_args["input-data"] == base64.b64encode(b"print(1)\n").decode("ascii")


def test_run_without_input_data_sets_no_input_field() -> None:
    agent = _FakeAgent(exitcode=0, out=b"")
    _exec(agent).run(_DOMAIN, ["/usr/bin/curl", "-fsS", "https://store/obj"])
    assert "input-data" not in agent.commands[0]["arguments"]


def test_run_returns_captured_stdout_and_exit_status() -> None:
    agent = _FakeAgent(exitcode=0, out=b"published-object-bytes")
    result = _exec(agent).run(_DOMAIN, ["/usr/bin/curl", "-fsS", "https://store/obj"])
    assert result.exit_status == 0
    assert result.stdout == b"published-object-bytes"
    assert result.stderr == b""
    issued = [c["execute"] for c in agent.commands]
    assert issued == ["guest-exec", "guest-exec-status"]
    exec_args = agent.commands[0]["arguments"]
    assert exec_args["path"] == "/usr/bin/curl"
    assert exec_args["arg"] == ["-fsS", "https://store/obj"]
    assert exec_args["capture-output"] is True
    # The status poll addresses the pid returned by guest-exec under the qmp keys.
    status_args = agent.commands[1]["arguments"]
    assert status_args == {"pid": 4242}
    # Both round-trips forward the real domain handle and the non-blocking flag bits (0).
    assert agent.domains == [_DOMAIN, _DOMAIN]
    assert agent.flags == [0, 0]


def test_run_captures_stderr_from_the_err_data_field() -> None:
    # stderr is decoded from the agent's ``err-data`` capture; reading any other key
    # would silently drop the in-guest error output.
    agent = _FakeAgent(exitcode=2, out=b"stdout-bytes", err=b"stderr-bytes")
    result = _exec(agent).run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert result.exit_status == 2
    assert result.stdout == b"stdout-bytes"
    assert result.stderr == b"stderr-bytes"


def test_run_polls_until_the_command_exits() -> None:
    agent = _FakeAgent(out=b"done", status_sequence=[False, False, True])
    result = _exec(agent).run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert result.stdout == b"done"
    assert [c["execute"] for c in agent.commands].count("guest-exec-status") == 3


def test_run_rejects_a_non_allowlisted_program() -> None:
    agent = _FakeAgent()
    with pytest.raises(CategorizedError) as excinfo:
        _exec(agent).run(_DOMAIN, ["/bin/sh", "-c", "curl https://store/obj"])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(excinfo.value) == "guest-agent exec program '/bin/sh' is not allowlisted"
    assert excinfo.value.details == {"program": "/bin/sh"}
    assert agent.commands == []  # rejected before any agent round-trip


def test_run_rejects_an_empty_argv() -> None:
    agent = _FakeAgent()
    with pytest.raises(CategorizedError) as excinfo:
        _exec(agent).run(_DOMAIN, [])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(excinfo.value) == "guest-agent exec requires a non-empty argv"
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
        _exec_raising(raised).run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert str(excinfo.value) == (
        "qemu-guest-agent command failed (agent unreachable or not connected)"
    )
    assert excinfo.value.details["libvirt_error"] == "guest agent is not connected"
    assert excinfo.value.details["libvirt_error_code"] is None
    assert excinfo.value.details["domain"] == "build-vm"


@pytest.mark.parametrize("code", _DETERMINISTIC_CODES)
def test_deterministic_libvirt_error_maps_to_configuration_error(code: int) -> None:
    # An agent that is not configured, denies the command, or cannot run it is a permanent
    # build-host condition; classify it CONFIGURATION_ERROR (retryable=false) so an agent does
    # not burn retry cycles on a failure that can never clear (#531, ADR-0159).
    with pytest.raises(CategorizedError) as excinfo:
        _exec_raising(libvirt_error(code)).run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(excinfo.value) == (
        "qemu-guest-agent is not usable on this build host "
        "(not configured, unsupported, denied, or unresponsive)"
    )
    assert excinfo.value.details["libvirt_error_code"] == code
    assert excinfo.value.details["libvirt_error"]  # the libvirt error string, non-empty
    assert excinfo.value.details["domain"] == "build-vm"


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
        _exec_raising(libvirt_error(code)).run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert excinfo.value.details["libvirt_error_code"] == code


def _rpc_disabled_error(message: str) -> libvirt.libvirtError:
    """Reproduce a qemu-ga allowlist denial as libvirt actually delivers it.

    qemu-ga answers the filtered RPC inside a healthy channel; libvirt's
    ``qemuAgentCheckError`` re-raises QEMU's ``error_setg`` text under the catch-all
    ``VIR_ERR_INTERNAL_ERROR`` (code 1), NOT under any deterministic-config code — which is
    precisely why #1631's denial read as a retryable transport failure.
    """
    err = libvirt.libvirtError(message)
    err.err = (libvirt.VIR_ERR_INTERNAL_ERROR, 0, message, 0, "", None, None, 0, 0)
    return err


@pytest.mark.parametrize(
    "message",
    [
        # The exact string the maintainer observed on every remote install against a Rocky 10
        # guest whose /etc/sysconfig/qemu-ga allowlist omitted guest-exec (#1631, #1610).
        "Command guest-exec has been disabled",
        # QEMU appends qemu-ga's disable reason when it has one, and libvirt prefixes its own
        # context; the match must survive both.
        "Command guest-exec has been disabled: the command is not allowed",
        "internal error: unable to execute QEMU agent command 'guest-exec': "
        "Command guest-exec has been disabled",
        "command guest-exec-status has been disabled",
    ],
)
def test_rpc_allowlist_denial_maps_to_configuration_error(message: str) -> None:
    # A denial is permanent — no retry can widen an allowlist — but it arrives under
    # VIR_ERR_INTERNAL_ERROR, so it must be caught by message, not by code (ADR-0483).
    with pytest.raises(CategorizedError) as excinfo:
        _exec_raising(_rpc_disabled_error(message)).run(
            _DOMAIN, ["/usr/bin/curl", "https://store/obj"]
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["libvirt_error_code"] == libvirt.VIR_ERR_INTERNAL_ERROR
    denied_rpc = excinfo.value.details["denied_rpc"]
    assert denied_rpc in {"guest-exec", "guest-exec-status"}
    # The message names the RPC and the file an operator must edit, so the failure is
    # actionable without reading the worker log (#1631).
    assert isinstance(denied_rpc, str)
    assert denied_rpc in str(excinfo.value)
    assert "--allow-rpcs" in str(excinfo.value)


def test_internal_error_without_a_denial_stays_transport_failure() -> None:
    # The guard against over-broad detection: VIR_ERR_INTERNAL_ERROR is libvirt's catch-all and
    # covers transient conditions too. Only the disabled-command message flips the category —
    # adding the bare code to _DETERMINISTIC_CONFIG_CODES would have made every internal error
    # permanently fatal.
    raised = _rpc_disabled_error("internal error: connection closed while reading agent reply")

    with pytest.raises(CategorizedError) as excinfo:
        _exec_raising(raised).run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert "denied_rpc" not in excinfo.value.details


def test_rpc_denial_is_not_retryable_at_the_queue() -> None:
    # The end of the #1631 chain: classifying the denial is only half the fix. Assert the
    # category the classifier picks is one the job queue dead-letters, so the denial cannot
    # burn DEFAULT_MAX_ATTEMPTS however it reaches the worker (ADR-0483).
    with pytest.raises(CategorizedError) as excinfo:
        _exec_raising(_rpc_disabled_error("Command guest-exec has been disabled")).run(
            _DOMAIN, ["/usr/bin/curl", "https://store/obj"]
        )
    assert retryable_category(excinfo.value.category) is False


def test_code_86_stays_transport_failure() -> None:
    # ADR-0159's global contract: AGENT_UNRESPONSIVE (code 86) is the mid-boot transient a retry
    # clears, so it stays retryable transport_failure for every caller of this seam.
    exc = _exec_raising(libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE))
    with pytest.raises(CategorizedError) as excinfo:
        exc.run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
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
        qemu_agent_command(_DOMAIN, "{}", 1, 0)

    assert excinfo.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert str(excinfo.value) == ("libvirt_qemu binding is required for qemu-guest-agent commands")
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
        qemu_agent_command(_DOMAIN, "{}", 1, 0)

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
        exc.run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "guest agent returned a non-JSON reply"


def test_agent_calls_use_a_bounded_positive_timeout() -> None:
    # A blocking (-2) timeout would let a disconnected agent wedge the worker; each
    # call must carry a positive bound so the seam's deadline governs total time.
    agent = _FakeAgent(out=b"ok")
    _exec(agent).run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert agent.timeouts  # at least one round-trip happened
    assert all(timeout > 0 for timeout in agent.timeouts)


def test_signal_killed_command_is_not_reported_as_success() -> None:
    # guest-exec-status returns `signal` (no exitcode) when the process is killed
    # (OOM, timeout-kill, SIGSEGV); defaulting a missing exitcode to 0 would read
    # a killed install as success.
    agent = _FakeAgent(exitcode=None, signal=9, out=b"partial")
    result = _exec(agent).run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert result.exit_status != 0
    assert result.exit_status == 128 + 9


def test_exited_with_neither_exitcode_nor_signal_is_not_success() -> None:
    # A `guest-exec-status` reply that reports `exited: true` but carries neither
    # `exitcode` nor `signal` is abnormal — the agent normally reports exactly one for
    # a reaped process. Defaulting it to 0 masks a command of unknown outcome as a pass
    # (issue #517), so it must raise INFRASTRUCTURE_FAILURE rather than return success.
    agent = _FakeAgent(exitcode=None, signal=None, out=b"partial")
    with pytest.raises(CategorizedError) as excinfo:
        _exec(agent).run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == (
        "guest agent reported a process exit without an exit code or signal"
    )


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
        exc.run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert str(excinfo.value) == "in-guest command did not exit within 6s"
    assert excinfo.value.details == {"domain": "build-vm", "timeout_s": 6.0}


def test_run_times_out_exactly_at_the_deadline() -> None:
    # The deadline check is ``>=``: once the clock reaches the deadline value the poll
    # loop must stop. A clock pinned at the deadline would loop forever under a strict
    # ``>`` comparison, so the bounded iterator exhausting would surface a different
    # failure than the timeout error this asserts.
    agent = _FakeAgent(status_sequence=[False] * 50)
    exc = GuestAgentExec(
        agent_command=agent,
        allowed_programs=_ALLOWED,
        timeout_s=4.0,
        sleep=lambda _s: None,
        monotonic=iter([0.0, 4.0, 4.0]).__next__,
    )
    with pytest.raises(CategorizedError) as excinfo:
        exc.run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert str(excinfo.value) == "in-guest command did not exit within 4s"


def test_run_sleeps_the_configured_poll_interval_between_polls() -> None:
    # Each not-yet-exited poll waits the configured poll interval; the seam must pass the
    # concrete poll_s (not None) to the injected sleep so a real run paces its round-trips.
    slept: list[float] = []
    agent = _FakeAgent(out=b"done", status_sequence=[False, False, True])
    exc = GuestAgentExec(
        agent_command=agent,
        allowed_programs=_ALLOWED,
        poll_s=0.25,
        sleep=slept.append,
        monotonic=_float_clock(),
    )
    exc.run(_DOMAIN, ["/usr/bin/curl", "https://store/obj"])
    assert slept == [0.25, 0.25]
