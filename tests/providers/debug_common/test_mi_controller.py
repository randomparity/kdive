"""pygdbmi controller adapter tests without spawning gdb."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.debug_common.gdbmi.core.mi_controller import (
    PygdbmiController,
    timeout_error,
)


class _FakeGdbController:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.writes: list[tuple[str, float, bool]] = []
        self.reads: list[tuple[float, bool]] = []
        self.exited = False
        self.gdb_process: _FakeGdbController | None = self
        self.stdin = self.stdout = self.stderr = None

    def write(
        self,
        command: str,
        *,
        timeout_sec: float,
        raise_error_on_timeout: bool,
    ) -> list[dict[str, object]]:
        self.writes.append((command, timeout_sec, raise_error_on_timeout))
        if self.timeout:
            from pygdbmi.constants import GdbTimeoutError

            raise GdbTimeoutError("timed out")
        return [{"type": "result", "message": "done"}]

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        self.reads.append((timeout_sec, raise_error_on_timeout))
        if self.timeout:
            from pygdbmi.constants import GdbTimeoutError

            raise GdbTimeoutError("timed out")
        return [{"type": "notify", "message": "stopped"}]

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.exited = True

    def wait(self, timeout: float) -> int:
        return 0


def _controller(fake: _FakeGdbController) -> PygdbmiController:
    controller = object.__new__(PygdbmiController)
    controller._controller = fake
    return controller


def test_timeout_error_preserves_command_timeout_and_transport_code() -> None:
    error = timeout_error("-exec-continue", 1.5)

    assert error.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(error) == "gdb/MI command timed out after 1.5s: -exec-continue"
    assert error.details == {
        "code": "transport_stall",
        "command": "-exec-continue",
        "timeout_seconds": 1.5,
    }


def test_write_forwards_timeout_and_requests_timeout_errors() -> None:
    fake = _FakeGdbController()
    controller = _controller(fake)

    records = controller.write("-break-list", timeout_sec=2.0)

    assert records == [{"type": "result", "message": "done"}]
    assert fake.writes == [("-break-list", 2.0, True)]


def test_write_maps_pygdbmi_timeout_to_categorized_error() -> None:
    controller = _controller(_FakeGdbController(timeout=True))

    with pytest.raises(CategorizedError) as exc_info:
        controller.write("-target-select remote 127.0.0.1:1234", timeout_sec=0.25)

    assert exc_info.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc_info.value.details["command"] == "-target-select remote 127.0.0.1:1234"
    # The real timeout value (not None) must be carried into the error.
    assert exc_info.value.details["timeout_seconds"] == 0.25


def test_read_uses_non_raising_poll() -> None:
    fake = _FakeGdbController()
    controller = _controller(fake)

    assert controller.read(timeout_sec=0.1) == [{"type": "notify", "message": "stopped"}]
    assert fake.reads == [(0.1, False)]


def test_get_gdb_response_maps_or_suppresses_timeout() -> None:
    controller = _controller(_FakeGdbController(timeout=True))

    with pytest.raises(CategorizedError) as exc_info:
        controller.get_gdb_response(timeout_sec=0.5)

    assert exc_info.value.details["command"] == "get_gdb_response"
    assert exc_info.value.details["timeout_seconds"] == 0.5
    assert controller.get_gdb_response(timeout_sec=0.5, raise_error_on_timeout=False) == []


def test_exit_delegates_to_underlying_controller() -> None:
    fake = _FakeGdbController()
    controller = _controller(fake)

    controller.exit()

    assert fake.exited is True


@pytest.mark.parametrize("last_wait_fails", [False, True])
def test_exit_escalates_and_requires_confirmed_wait(last_wait_fails: bool) -> None:
    import subprocess
    from unittest.mock import Mock, call

    process = Mock()
    process.poll.return_value = None
    process.stdin = process.stdout = process.stderr = None
    process.wait.side_effect = [
        subprocess.TimeoutExpired("gdb", 2),
        subprocess.TimeoutExpired("gdb", 2) if last_wait_fails else 0,
    ]
    controller = object.__new__(PygdbmiController)
    controller._controller = Mock(gdb_process=process)
    if last_wait_fails:
        with pytest.raises(CategorizedError):
            controller.exit()
        assert controller._controller.gdb_process is process
    else:
        controller.exit()
        assert controller._controller.gdb_process is None
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_args_list == [call(timeout=2.0), call(timeout=2.0)]
