"""Attested stdlib-only pre-gate entrypoint for capture operations (ADR-0558)."""

from __future__ import annotations

import os
import sys


def _arguments(argv: list[str]) -> tuple[str, int]:
    if len(argv) != 4 or argv[0] != "--launch-token" or argv[2] != "--gate-fd":
        raise ValueError("expected --launch-token TOKEN --gate-fd FD")
    token = argv[1]
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise ValueError("launch token must be 64 lowercase hexadecimal characters")
    try:
        gate_fd = int(argv[3])
    except ValueError as error:
        raise ValueError("gate fd must be an integer") from error
    if gate_fd < 3:
        raise ValueError("gate fd must not alias standard streams")
    return token, gate_fd


def main(argv: list[str] | None = None) -> int:
    """Install containment, acknowledge it, then block before importing request code."""
    launch_token, gate_fd = _arguments(sys.argv[1:] if argv is None else argv)
    from kdive.jobs.capture_operations.process.sandbox import install_capture_filter

    install_capture_filter()
    os.write(sys.stdout.fileno(), b"F")
    devnull_fd = os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC)
    try:
        os.dup2(devnull_fd, sys.stdout.fileno())
    finally:
        os.close(devnull_fd)
    release = os.read(gate_fd, 1)
    if release == b"":
        return 0
    if release != b"R":
        raise RuntimeError("invalid capture gate release byte")
    from kdive.jobs.capture_operations.child import run_capture_child

    return run_capture_child(launch_token, gate_fd)


if __name__ == "__main__":
    raise SystemExit(main())
