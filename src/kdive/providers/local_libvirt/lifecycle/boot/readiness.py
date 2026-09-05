"""Console and domstate readiness probes for local-libvirt boots."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess  # noqa: S404 - virsh domstate uses fixed argv, no shell  # nosec B404
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple
from uuid import UUID

import kdive.config as config
from kdive.domain.lifecycle.crash_signatures import first_crash_signature
from kdive.providers.local_libvirt.lifecycle.storage import _open_validated_console_log
from kdive.providers.local_libvirt.settings import LIBVIRT_BOOT_WINDOW_S, LIBVIRT_URI
from kdive.providers.shared.runtime_paths import console_log_path, domain_name_for, read_console_log

_POLL_INTERVAL_SECONDS = 5.0
_DOMSTATE_PROBE_TIMEOUT = 10
_TERMINAL_DOMSTATES = frozenset({"shut off", "crashed"})
_VIRSH = "virsh"

_READINESS_MARKER = "kdive-ready"
_MAX_CONSOLE_WINDOW_BYTES = 2 * 1024 * 1024

_log = logging.getLogger(__name__)


class ConsoleVerdict(StrEnum):
    READY = "ready"
    CRASHED = "crashed"
    PENDING = "pending"


class ProbeFailure(StrEnum):
    """Why a ``virsh domstate`` probe failed, as a closed agent-facing vocabulary (ADR-0594).

    ``VIRSH_MISSING`` also covers every ENOENT raised by the exec, not only an absent binary:
    ``OSError(2, ...)`` is a ``FileNotFoundError``, whose arm precedes the ``OSError`` arm.
    """

    VIRSH_MISSING = "virsh_missing"
    VIRSH_TIMEOUT = "virsh_timeout"
    VIRSH_PROBE_FAILED = "virsh_probe_failed"
    VIRSH_NONZERO_EXIT = "virsh_nonzero_exit"


class ReadinessResult(NamedTuple):
    """The run-readiness preflight result: did the System answer, and did its checks pass."""

    answered: bool
    ok: bool
    probe_error: ProbeFailure | None = None


class _DomainExitProbe(NamedTuple):
    """The domstate probe result plus its classified probe-failure reason."""

    exited: bool
    error: ProbeFailure | None = None


class _ConsoleWindowFailure(RuntimeError):
    """The prepared console window no longer proves append continuity."""


class ConsoleReadinessWindow:
    """One bounded retained console inode and its pre-create deadline (ADR-0600)."""

    def __init__(
        self,
        path: Path,
        descriptor: int,
        *,
        deadline: float,
        max_bytes: int = _MAX_CONSOLE_WINDOW_BYTES,
    ) -> None:
        identity = os.fstat(descriptor)
        self._path = path
        self._descriptor: int | None = descriptor
        self._identity = (identity.st_dev, identity.st_ino)
        self._observed = b""
        self.deadline = deadline
        self._max_bytes = max_bytes

    def read(self) -> bytes:
        descriptor = self._require_open()
        try:
            if not self._identity_matches(descriptor):
                raise _ConsoleWindowFailure("console readiness window changed")
            data = os.pread(descriptor, self._max_bytes + 1, 0)
            if not self._identity_matches(descriptor):
                raise _ConsoleWindowFailure("console readiness window changed")
        except OSError as exc:
            raise _ConsoleWindowFailure("console readiness window changed") from exc
        if len(data) > self._max_bytes:
            raise _ConsoleWindowFailure("console readiness window exceeds its byte bound")
        if not data.startswith(self._observed):
            raise _ConsoleWindowFailure("console readiness window changed")
        self._observed = data
        return data

    def _identity_matches(self, descriptor: int) -> bool:
        current = os.fstat(descriptor)
        named = os.stat(self._path, follow_symlinks=False)
        return (
            (current.st_dev, current.st_ino)
            == self._identity
            == (
                named.st_dev,
                named.st_ino,
            )
        )

    def close(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)

    def _require_open(self) -> int:
        if self._descriptor is None:
            raise RuntimeError("console readiness window is closed")
        return self._descriptor


def prepare_console_readiness_window(system_id: UUID) -> ConsoleReadinessWindow:
    """Truncate and retain the validated console inode for one external boot."""
    path = console_log_path(system_id)
    descriptor = _open_validated_console_log(path, os.O_RDWR)
    try:
        os.ftruncate(descriptor, 0)
        deadline = time.monotonic() + config.require(LIBVIRT_BOOT_WINDOW_S)
        return ConsoleReadinessWindow(path, descriptor, deadline=deadline)
    except BaseException:
        os.close(descriptor)
        raise


def classify_console(data: bytes, *, marker: str = _READINESS_MARKER) -> ConsoleVerdict:
    """Classify a console capture as ready, crashed, or pending."""
    text = data.decode("utf-8", errors="replace")
    marker_re = re.compile(rf"(?:^|[^\S\n]){re.escape(marker)}[^\S\n]*$", re.MULTILINE)
    marker_match = marker_re.search(text)
    region = text if marker_match is None else text[: marker_match.start()]
    if first_crash_signature(region) is not None:
        return ConsoleVerdict.CRASHED
    return ConsoleVerdict.READY if marker_match is not None else ConsoleVerdict.PENDING


def _bounded_probe_error(message: str) -> str:
    return message[:200]


def _probe_failed(domain_name: str, failure: ProbeFailure, detail: str) -> _DomainExitProbe:
    """Log the bounded diagnostic for the operator and return the classified failure (ADR-0594)."""
    _log.warning(
        "domstate probe failed for %s (%s): %s",
        domain_name,
        failure.value,
        _bounded_probe_error(detail),
    )
    return _DomainExitProbe(False, failure)


def _domain_exit_probe(domain_name: str) -> _DomainExitProbe:  # pragma: no cover - live_vm
    """Return whether ``virsh domstate`` reports terminal state plus its classified failure."""
    uri = config.require(LIBVIRT_URI)
    virsh = shutil.which(_VIRSH)
    if virsh is None:
        return _probe_failed(domain_name, ProbeFailure.VIRSH_MISSING, "virsh executable not found")
    try:
        proc = subprocess.run(  # noqa: S603 - virsh argv; URI/domain are data  # nosec B603
            [virsh, "-c", uri, "domstate", domain_name],
            capture_output=True,
            text=True,
            timeout=_DOMSTATE_PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _probe_failed(
            domain_name,
            ProbeFailure.VIRSH_TIMEOUT,
            f"virsh domstate timed out after {exc.timeout:g}s",
        )
    except FileNotFoundError as exc:
        # Every ENOENT from the exec lands here, including one naming a transport socket path
        # rather than the binary. The member stays VIRSH_MISSING; the log keeps the detail.
        return _probe_failed(
            domain_name, ProbeFailure.VIRSH_MISSING, f"virsh domstate probe failed: {exc}"
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return _probe_failed(
            domain_name, ProbeFailure.VIRSH_PROBE_FAILED, f"virsh domstate probe failed: {exc}"
        )
    if proc.stdout.strip().lower() in _TERMINAL_DOMSTATES:
        return _DomainExitProbe(True)
    stderr = proc.stderr.strip().lower()
    exited = (
        proc.returncode != 0
        and domain_name.startswith("kdive-")
        and "failed to get domain" in stderr
    )
    if exited:
        return _DomainExitProbe(True)
    if proc.returncode != 0:
        return _probe_failed(
            domain_name,
            ProbeFailure.VIRSH_NONZERO_EXIT,
            stderr or f"virsh domstate exited {proc.returncode}",
        )
    return _DomainExitProbe(False)


class LocalExternalBootReadiness:
    """Poll one prepared external-boot console window to its fixed deadline (ADR-0600)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        domain_exit_probe: Callable[[str], _DomainExitProbe] = _domain_exit_probe,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._domain_exit_probe = domain_exit_probe

    def __call__(self, system_id: UUID, window: ConsoleReadinessWindow) -> ReadinessResult:
        first_probe_error: ProbeFailure | None = None
        domain_name = domain_name_for(system_id)
        while self._clock() < window.deadline:
            try:
                verdict = classify_console(window.read())
            except _ConsoleWindowFailure:
                return ReadinessResult(answered=True, ok=False)
            result = _verdict_to_result(verdict, exited=False)
            if result is not None:
                return result
            probe = self._domain_exit_probe(domain_name)
            if first_probe_error is None and probe.error is not None:
                first_probe_error = probe.error
            if probe.exited:
                try:
                    final = classify_console(window.read())
                except _ConsoleWindowFailure:
                    return ReadinessResult(True, False, first_probe_error)
                return _verdict_to_result(final, exited=True) or ReadinessResult(
                    True, False, first_probe_error
                )
            remaining = window.deadline - self._clock()
            if remaining <= 0:
                break
            self._sleep(min(_POLL_INTERVAL_SECONDS, remaining))
        return ReadinessResult(False, False, first_probe_error)


def _domain_exited(domain_name: str) -> bool:  # pragma: no cover - live_vm
    """True only if ``virsh domstate`` reports a terminal state."""
    return _domain_exit_probe(domain_name).exited


def _verdict_to_result(verdict: ConsoleVerdict, *, exited: bool) -> ReadinessResult | None:
    """Map a console verdict plus domain-exited flag to a readiness result, or ``None``."""
    if verdict is ConsoleVerdict.READY:
        return ReadinessResult(answered=True, ok=True)
    if verdict is ConsoleVerdict.CRASHED:
        return ReadinessResult(answered=True, ok=False)
    if exited:
        return ReadinessResult(answered=True, ok=False)
    return None


def _real_readiness(system_id: UUID) -> ReadinessResult:  # pragma: no cover - live_vm
    """Run one readiness probe of the System's truncated console."""
    log_path = console_log_path(system_id)
    result = _verdict_to_result(classify_console(read_console_log(log_path)), exited=False)
    if result is not None:
        return result
    probe = _domain_exit_probe(domain_name_for(system_id))
    if probe.exited:
        return _verdict_to_result(
            classify_console(read_console_log(log_path)), exited=True
        ) or ReadinessResult(answered=True, ok=False)
    time.sleep(_POLL_INTERVAL_SECONDS)
    return ReadinessResult(answered=False, ok=False, probe_error=probe.error)
