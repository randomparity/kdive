"""Read guest fault-injection knob state over loopback SSH (ADR-0825, ADR-0822).

Runs the reviewed in-guest ``kdive-faultinject read`` with fixed argv and no caller text, then
redacts, byte-caps and strictly validates the reply into integers. Guest stderr is never returned.
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - fixed ssh argv, no shell; helper validated  # nosec B404
from collections.abc import Callable, Mapping

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.debug.live_introspect import _live_ssh_argv
from kdive.providers.ports.fault_injection import (
    FAULT_KNOB_PARAMETERS,
    REPLY_BYTE_CAP,
    FaultKnobState,
    validate_knob,
    validate_set_request,
)
from kdive.providers.shared.ssh_connect_retry import (
    SshRetryPolicy,
    classify_ssh_failure,
    run_ssh_with_retry,
)
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

_HELPER = "/usr/local/sbin/kdive-faultinject"
_SSH_TIMEOUT_S = 15
_SSH_RETRY = SshRetryPolicy(deadline_s=10.0)
_EXIT_DEBUGFS_NOT_MOUNTED = 3
_EXIT_KNOB_ABSENT = 4
_EXIT_COMMAND_NOT_FOUND = 127

# (transport_handle, key_path, helper argv) -> (helper exit status, stdout). Raises
# CategorizedError on an ssh-level fault (exit 255, timeout, launch failure).
type _RunHelper = Callable[[str, str, list[str]], tuple[int, bytes]]

_REMEDIATION = {
    "debugfs_not_mounted": (
        "debugfs is not mounted in the guest; mount it (mount -t debugfs none /sys/kernel/debug) "
        "or boot an image that mounts it."
    ),
    "fault_injection_unsupported": (
        "the guest kernel exposes neither failslab nor fail_page_alloc; boot a kernel built with "
        "CONFIG_FAULT_INJECTION, CONFIG_FAILSLAB and CONFIG_FAIL_PAGE_ALLOC."
    ),
    "knob_absent": (
        "the guest kernel does not expose the named knob; boot a kernel built with "
        "CONFIG_FAULT_INJECTION and CONFIG_FAILSLAB (failslab) or CONFIG_FAIL_PAGE_ALLOC "
        "(fail_page_alloc), or name the other knob."
    ),
    "helper_not_installed": (
        "/usr/local/sbin/kdive-faultinject is missing from the guest image; rebuild the debug "
        "image with the helper staged (ADR-0822) and reprovision the System."
    ),
}


def _config_error(reason: str) -> CategorizedError:
    return CategorizedError(
        _REMEDIATION[reason],
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": reason},
    )


def _infra_error(reason: str, exit_status: int | None = None) -> CategorizedError:
    details: dict[str, object] = {"reason": reason}
    if exit_status is not None:
        details["exit_status"] = exit_status
    return CategorizedError(
        "the guest fault-injection helper did not return a usable reply",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=details,
    )


def _parse_knob(knob: str, raw: object) -> FaultKnobState:
    if not isinstance(raw, dict) or type(raw.get("present")) is not bool:
        raise _infra_error("helper_output_invalid")
    if not raw["present"]:
        if set(raw) != {"present"}:
            raise _infra_error("helper_output_invalid")
        return FaultKnobState(present=False, parameters={})
    names = FAULT_KNOB_PARAMETERS[knob]
    if set(raw) != {"present", *names}:
        raise _infra_error("helper_output_invalid")
    values = {name: raw[name] for name in names}
    if any(type(value) is not int for value in values.values()):
        raise _infra_error("helper_output_invalid")
    return FaultKnobState(present=True, parameters=values)


def parse_helper_state(
    text: str, expected: tuple[str, ...] = tuple(FAULT_KNOB_PARAMETERS)
) -> dict[str, FaultKnobState]:
    """Validate ``{"knobs":{...}}`` into integers; a reply with every knob absent is unsupported.

    ``expected`` is the exact set of knob names the call addressed (both for ``read``).
    """
    try:
        decoded = json.loads(text)
    except ValueError as exc:
        raise _infra_error("helper_output_invalid") from exc
    is_envelope = isinstance(decoded, dict) and set(decoded) == {"knobs"}
    knobs = decoded["knobs"] if is_envelope else None
    if not isinstance(knobs, dict) or set(knobs) != set(expected):
        raise _infra_error("helper_output_invalid")
    state = {knob: _parse_knob(knob, knobs[knob]) for knob in expected}
    if not any(knob.present for knob in state.values()):
        raise _config_error("fault_injection_unsupported")
    return state


def _real_run_helper(  # pragma: no cover - live_vm
    transport_handle: str, key_path: str, secret_registry: SecretRegistry, helper_args: list[str]
) -> tuple[int, bytes]:
    """SSH-exec ``kdive-faultinject`` with fixed ``helper_args``; return ``(exit status, stdout)``.

    Both ``set`` and ``reset`` are idempotent, so retrying a retryable ssh failure (exit 255) is
    safe even when the connection closed after the helper had started.
    """
    argv = _live_ssh_argv(transport_handle, secret_registry, helper_args, key_path, program=_HELPER)

    def run_once() -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(  # noqa: S603 - fixed argv; no caller text  # nosec B603
                argv, timeout=_SSH_TIMEOUT_S, check=False, capture_output=True
            )
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                (
                    "the fault-injection helper call exceeded the ssh timeout; "
                    "the guest state is unknown"
                ),
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"reason": "timeout", "timeout_s": _SSH_TIMEOUT_S},
            ) from exc
        except OSError as exc:
            raise CategorizedError(
                "could not launch ssh for the fault-injection helper call",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"reason": "launch_failed"},
            ) from exc

    proc = run_ssh_with_retry(run_once, policy=_SSH_RETRY)
    if proc.returncode == 255:
        raise CategorizedError(
            "ssh to the guest failed; the fault-injection helper may not have run",
            category=ErrorCategory.TRANSPORT_FAILURE,
            details={"reason": classify_ssh_failure(255, proc.stderr), "exit_status": 255},
        )
    return proc.returncode, proc.stdout


class LocalLibvirtFaultInjection:
    """The realized `FaultInjectionInspector` for local-libvirt Systems (ADR-0825, ADR-0826)."""

    def __init__(
        self, *, secret_registry: SecretRegistry, run_helper: _RunHelper | None = None
    ) -> None:
        self._secret_registry = secret_registry
        self._run_helper: _RunHelper = run_helper or (
            lambda handle, key, args: _real_run_helper(handle, key, secret_registry, args)
        )

    def read_state(self, *, transport_handle: str, key_path: str) -> dict[str, FaultKnobState]:
        return self._call(transport_handle, key_path, ["read"], tuple(FAULT_KNOB_PARAMETERS))

    def set_knob(
        self, *, transport_handle: str, key_path: str, knob: str, parameters: Mapping[str, int]
    ) -> dict[str, FaultKnobState]:
        validate_set_request(knob, parameters)
        pairs = [
            f"{name}={parameters[name]}"
            for name in FAULT_KNOB_PARAMETERS[knob]
            if name in parameters
        ]
        return self._call(transport_handle, key_path, ["set", knob, *pairs], (knob,), knob=knob)

    def reset(
        self, *, transport_handle: str, key_path: str, knob: str | None
    ) -> dict[str, FaultKnobState]:
        if knob is not None:
            validate_knob(knob)
        expected = (knob,) if knob is not None else tuple(FAULT_KNOB_PARAMETERS)
        args = ["reset", knob] if knob is not None else ["reset"]
        return self._call(transport_handle, key_path, args, expected, knob=knob)

    def _call(
        self,
        transport_handle: str,
        key_path: str,
        helper_args: list[str],
        expected: tuple[str, ...],
        *,
        knob: str | None = None,
    ) -> dict[str, FaultKnobState]:
        exit_status, stdout = self._run_helper(transport_handle, key_path, helper_args)
        if exit_status == _EXIT_DEBUGFS_NOT_MOUNTED:
            raise _config_error("debugfs_not_mounted")
        if exit_status == _EXIT_KNOB_ABSENT:
            raise _config_error(
                "knob_absent" if knob is not None else "fault_injection_unsupported"
            )
        if exit_status == _EXIT_COMMAND_NOT_FOUND:
            raise _config_error("helper_not_installed")
        if exit_status != 0:
            raise _infra_error("helper_failed", exit_status)
        if len(stdout) > REPLY_BYTE_CAP:
            raise _infra_error("helper_output_too_large")
        text = Redactor(registry=self._secret_registry).redact_text(
            stdout.decode("utf-8", "replace")
        )
        return parse_helper_state(text, expected)
