"""pygdbmi subprocess adapter for the local-libvirt debug provider."""

from __future__ import annotations

import subprocess

from pygdbmi.constants import GdbTimeoutError

from kdive.domain.errors import CategorizedError, ErrorCategory


def timeout_error(command: str, timeout_sec: float) -> CategorizedError:
    """The error an MI write timeout raises."""
    return CategorizedError(
        f"gdb/MI command timed out after {timeout_sec}s: {command}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"code": "transport_stall", "command": command, "timeout_seconds": timeout_sec},
    )


class PygdbmiController:  # pragma: no cover - live_vm
    """Real ``GdbController``: a managed ``gdb --interpreter=mi3`` child via ``pygdbmi``."""

    def __init__(self, command: list[str]) -> None:
        from pygdbmi.gdbcontroller import GdbController

        self._controller = GdbController(command=command)

    def write(self, command: str, *, timeout_sec: float) -> list[dict[str, object]]:
        try:
            return self._controller.write(
                command, timeout_sec=timeout_sec, raise_error_on_timeout=True
            )
        except GdbTimeoutError as exc:
            raise timeout_error(command, timeout_sec) from exc

    def read(self, *, timeout_sec: float) -> list[dict[str, object]]:
        return self.get_gdb_response(timeout_sec=timeout_sec, raise_error_on_timeout=False)

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        try:
            return self._controller.get_gdb_response(
                timeout_sec=timeout_sec, raise_error_on_timeout=raise_error_on_timeout
            )
        except GdbTimeoutError as exc:
            if raise_error_on_timeout:
                raise timeout_error("get_gdb_response", timeout_sec) from exc
            return []

    def exit(self) -> None:
        process = self._controller.gdb_process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
            self._controller.gdb_process = None
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        except Exception as exc:
            raise CategorizedError(
                "could not confirm GDB controller cleanup",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
