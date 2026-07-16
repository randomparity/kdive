"""Console and domstate readiness probes for local-libvirt boots."""

from __future__ import annotations

import re
import shutil
import subprocess  # noqa: S404 - virsh domstate uses fixed argv, no shell  # nosec B404
import time
from enum import StrEnum
from typing import NamedTuple
from uuid import UUID

import kdive.config as config
from kdive.domain.lifecycle.crash_signatures import first_crash_signature
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.shared.runtime_paths import console_log_path, domain_name_for, read_console_log

_POLL_INTERVAL_SECONDS = 5.0
_DOMSTATE_PROBE_TIMEOUT = 10
_TERMINAL_DOMSTATES = frozenset({"shut off", "crashed"})
_VIRSH = "virsh"

_READINESS_MARKER = "kdive-ready"


class ConsoleVerdict(StrEnum):
    READY = "ready"
    CRASHED = "crashed"
    PENDING = "pending"


class ReadinessResult(NamedTuple):
    """The run-readiness preflight result: did the System answer, and did its checks pass."""

    answered: bool
    ok: bool
    probe_error: str | None = None


class _DomainExitProbe(NamedTuple):
    """The domstate probe result plus a bounded probe-failure diagnostic."""

    exited: bool
    error: str | None = None


def classify_console(data: bytes, *, marker: str = _READINESS_MARKER) -> ConsoleVerdict:
    """Classify a console capture as ready, crashed, or pending."""
    text = data.decode("utf-8", errors="replace")
    marker_re = re.compile(rf"^[^\S\n]*{re.escape(marker)}[^\S\n]*$", re.MULTILINE)
    marker_match = marker_re.search(text)
    region = text if marker_match is None else text[: marker_match.start()]
    if first_crash_signature(region) is not None:
        return ConsoleVerdict.CRASHED
    return ConsoleVerdict.READY if marker_match is not None else ConsoleVerdict.PENDING


def _bounded_probe_error(message: str) -> str:
    return message[:200]


def _domain_exit_probe(domain_name: str) -> _DomainExitProbe:  # pragma: no cover - live_vm
    """Return whether ``virsh domstate`` reports terminal state plus probe diagnostics."""
    uri = config.require(LIBVIRT_URI)
    virsh = shutil.which(_VIRSH)
    if virsh is None:
        return _DomainExitProbe(False, "virsh executable not found")
    try:
        proc = subprocess.run(  # noqa: S603 - virsh argv; URI/domain are data  # nosec B603
            [virsh, "-c", uri, "domstate", domain_name],
            capture_output=True,
            text=True,
            timeout=_DOMSTATE_PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _DomainExitProbe(
            False,
            f"virsh domstate timed out after {exc.timeout:g}s",
        )
    except FileNotFoundError:
        return _DomainExitProbe(False, "virsh executable not found")
    except (subprocess.SubprocessError, OSError) as exc:
        return _DomainExitProbe(False, _bounded_probe_error(f"virsh domstate probe failed: {exc}"))
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
        error = stderr or f"virsh domstate exited {proc.returncode}"
        return _DomainExitProbe(False, _bounded_probe_error(error))
    return _DomainExitProbe(False)


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
