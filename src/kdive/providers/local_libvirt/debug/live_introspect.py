"""Live drgn-over-SSH introspection for local-libvirt Systems (ADR-0219)."""

from __future__ import annotations

import ipaddress
import json
import subprocess  # noqa: S404 - fixed ssh argv, no shell; helper validated  # nosec B404
from collections.abc import Callable
from typing import cast

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.providers.ports.retrieve import IntrospectOutput, LiveIntrospector, LiveScriptOutput
from kdive.providers.shared.debug_common.introspect import (
    _REPORT_BYTE_CAP,
    assemble_report,
    assemble_script_output,
)
from kdive.providers.shared.ssh_connect_retry import (
    SshRetryPolicy,
    run_ssh_with_retry,
    ssh_failure_details,
)
from kdive.security.secrets.secret_registry import SecretRegistry

# The fixed live-helper set (ADR-0033 §2 / ADR-0085): the same three in-tree helpers as the
# offline path. There is no caller-supplied drgn script here; an unknown helper is rejected.
_LIVE_HELPERS = frozenset({"tasks", "modules", "sysinfo"})
_DRGN_HELPER = "/usr/local/sbin/kdive-drgn"
_LOOPBACK_HOST = "127.0.0.1"  # the live transport is loopback-only (ADR-0218 §1)
_SSH_USER = "root"  # the per-System bootstrap public key is injected to root (ADR-0289/0218 §1)
_LIVE_INTROSPECT_SSH_TIMEOUT_S = 60
_SSH_CONNECT_TIMEOUT_S = 10
_LIVE_SSH_RETRY = SshRetryPolicy(deadline_s=20.0)
_LIVE_SCRIPT_SSH_SLACK_S = 10.0

# (transport_handle, helper, key_path) -> the section dict the in-guest ``kdive-drgn <helper>``
# emits. ``key_path`` is the caller-materialized per-System bootstrap private key (ADR-0289).
type _RunLiveHelper = Callable[[str, str, str], dict[str, object]]
# (transport_handle, script, timeout_sec, key_path) -> the in-guest ``kdive-drgn run-script``
# stdout.
type _RunLiveScript = Callable[[str, str, float, str], str]


def _normalize_attach_error(exc: Exception, message: str) -> CategorizedError:
    """A categorized fault passes through; any other live fault becomes an attach failure."""
    if isinstance(exc, CategorizedError):
        return exc
    return CategorizedError(message, category=ErrorCategory.DEBUG_ATTACH_FAILURE)


class LocalLibvirtLiveIntrospect:
    """The realized live-introspection port (ADR-0219).

    Runs live drgn introspection by SSH-exec'ing the in-guest ``kdive-drgn <helper>`` helper over
    the drgn-live SSH transport (ADR-0218) and parsing its one-JSON-object section output, then
    redacting + byte-capping it through the shared ``assemble_report`` (the single redaction
    boundary). drgn runs **in the guest** against its own live ``/proc/kcore``; the worker only
    opens an SSH connection and parses JSON. This mirrors ``RemoteLibvirtLiveIntrospect`` exactly,
    differing only in the channel (SSH vs the qemu-guest-agent).

    The ``run_live_helper`` seam is ``None`` off-gate; ``introspect_live`` then raises
    ``MISSING_DEPENDENCY``, mirroring the offline port's seam guard. ``from_env`` wires the real
    ``_real_run_live_helper`` (its ``subprocess`` SSH call is the only ``live_vm`` seam; the handle
    validation and error mapping run in CI). Callers pass ``key_path`` — the per-System bootstrap
    private key (ADR-0289), loaded and materialized by the MCP tool boundary — into
    ``introspect_live``/``run_script``; this port never resolves or reads key material itself.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        run_live_helper: _RunLiveHelper | None = None,
        run_live_script: _RunLiveScript | None = None,
    ) -> None:
        self._secret_registry = secret_registry
        self._run_live_helper = run_live_helper
        self._run_live_script = run_live_script
        self._report_byte_cap = _REPORT_BYTE_CAP
        self._live_script_byte_cap = _REPORT_BYTE_CAP

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> LocalLibvirtLiveIntrospect:
        """Build from env with the real SSH-exec seams (opens no SSH and imports no drgn here)."""

        def _seam(transport_handle: str, helper: str, key_path: str) -> dict[str, object]:
            return _real_run_live_helper(
                transport_handle, helper, key_path, secret_registry=secret_registry
            )

        def _script_seam(
            transport_handle: str, script: str, timeout_sec: float, key_path: str
        ) -> str:
            return _real_run_live_script(
                transport_handle, script, timeout_sec, key_path, secret_registry=secret_registry
            )

        return cls(
            secret_registry=secret_registry,
            run_live_helper=_seam,
            run_live_script=_script_seam,
        )

    def run_script(
        self, *, transport_handle: str, script: str, timeout_sec: float, key_path: str
    ) -> LiveScriptOutput:
        """SSH-exec a caller drgn script in-guest over the drgn-live transport."""
        if self._run_live_script is None:
            raise CategorizedError(
                "live drgn script introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        try:
            stdout = self._run_live_script(transport_handle, script, timeout_sec, key_path)
        except Exception as exc:  # noqa: BLE001 - any seam fault becomes a typed failure
            raise _normalize_attach_error(
                exc, "drgn could not run the script in the live guest"
            ) from exc
        return assemble_script_output(
            stdout, byte_cap=self._live_script_byte_cap, secret_registry=self._secret_registry
        )

    def introspect_live(
        self, *, transport_handle: str, helper: str, key_path: str
    ) -> IntrospectOutput:
        """SSH-exec one in-guest helper over the drgn-live transport; return a redacted report."""
        if self._run_live_helper is None:
            raise CategorizedError(
                "live drgn introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        if helper not in _LIVE_HELPERS:
            raise CategorizedError(
                f"unknown live introspection helper: {helper}",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        try:
            section = self._run_live_helper(transport_handle, helper, key_path)
        except Exception as exc:  # noqa: BLE001 - any seam fault becomes a typed failure
            raise _normalize_attach_error(
                exc, "drgn could not attach to the live guest kernel"
            ) from exc
        tasks = section if helper == "tasks" else {}
        modules = section if helper == "modules" else {}
        sysinfo = section if helper == "sysinfo" else {}
        return assemble_report(
            tasks,
            modules,
            sysinfo,
            byte_cap=self._report_byte_cap,
            secret_registry=self._secret_registry,
        )


def _validate_ssh_target(transport_handle: str) -> int:
    """Decode the handle and return the loopback SSH port; raise CONFIGURATION_ERROR otherwise."""
    decoded = TransportHandleData.decode(transport_handle)
    if decoded.kind != "ssh":
        raise CategorizedError(
            f"live introspection handle must be an ssh transport, got {decoded.kind!r}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    try:
        is_loopback = ipaddress.ip_address(decoded.host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise CategorizedError(
            "live introspection ssh host must be a loopback IP literal",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return decoded.port


def _live_ssh_argv(
    transport_handle: str, secret_registry: SecretRegistry, drgn_args: list[str], key_path: str
) -> list[str]:
    """Build the fixed loopback SSH argv for an in-guest ``kdive-drgn`` invocation."""
    port = _validate_ssh_target(transport_handle)
    del secret_registry  # key content is registered by the caller (the tool boundary)
    return [
        "ssh",
        "-i",
        key_path,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ConnectTimeout={_SSH_CONNECT_TIMEOUT_S}",
        "-p",
        str(port),
        f"{_SSH_USER}@{_LOOPBACK_HOST}",
        "--",
        _DRGN_HELPER,
        *drgn_args,
    ]


def _real_run_live_helper(
    transport_handle: str, helper: str, key_path: str, *, secret_registry: SecretRegistry
) -> dict[str, object]:
    """SSH-exec ``kdive-drgn <helper>`` in the guest and return its section dict."""
    argv = _live_ssh_argv(transport_handle, secret_registry, [helper], key_path)
    return _exec_live_helper(argv)


def _raise_on_live_ssh_failure(proc: subprocess.CompletedProcess[bytes], message: str) -> None:
    """Raise a diagnosable ``DEBUG_ATTACH_FAILURE`` when drgn-live SSH exits non-zero (#1008)."""
    if proc.returncode != 0:
        raise CategorizedError(
            message,
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details=ssh_failure_details(proc.returncode, proc.stderr),
        )


def _exec_live_helper(argv: list[str]) -> dict[str, object]:  # pragma: no cover - live_vm
    """Run the fixed ssh argv and decode the in-guest helper's one JSON section object."""

    def run_once() -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(  # noqa: S603 - fixed argv; helper prevalidated  # nosec B603
                argv,
                timeout=_LIVE_INTROSPECT_SSH_TIMEOUT_S,
                check=False,
                capture_output=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                "live drgn introspection ssh round-trip exceeded the timeout",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"timeout_s": _LIVE_INTROSPECT_SSH_TIMEOUT_S},
            ) from exc
        except OSError as exc:
            raise CategorizedError(
                "could not launch ssh for live drgn introspection",
                category=ErrorCategory.TRANSPORT_FAILURE,
            ) from exc

    proc = run_ssh_with_retry(run_once, policy=_LIVE_SSH_RETRY)
    _raise_on_live_ssh_failure(
        proc, "in-guest drgn helper exited non-zero (could not attach to the live kernel)"
    )
    try:
        decoded = json.loads(proc.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CategorizedError(
            "in-guest drgn helper returned undecodable JSON",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
    if not isinstance(decoded, dict):
        raise CategorizedError(
            "in-guest drgn helper output was not a JSON object",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    return cast("dict[str, object]", decoded)


def _real_run_live_script(
    transport_handle: str,
    script: str,
    timeout_sec: float,
    key_path: str,
    *,
    secret_registry: SecretRegistry,
) -> str:
    """SSH-exec ``kdive-drgn run-script <timeout>`` with the script on stdin (ADR-0240)."""
    argv = _live_ssh_argv(
        transport_handle,
        secret_registry,
        ["run-script", str(max(1, int(timeout_sec)))],
        key_path,
    )
    return _exec_live_script(argv, script, timeout_sec + _LIVE_SCRIPT_SSH_SLACK_S)


def _exec_live_script(  # pragma: no cover - live_vm
    argv: list[str], script: str, ssh_timeout_s: float
) -> str:
    """Run the fixed ssh argv with the caller script on stdin; return its raw stdout."""

    def run_once() -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(  # noqa: S603 - fixed argv; script via stdin  # nosec B603
                argv,
                input=script.encode("utf-8"),
                timeout=ssh_timeout_s,
                check=False,
                capture_output=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                "live drgn script ssh round-trip exceeded the timeout",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"timeout_s": ssh_timeout_s},
            ) from exc
        except OSError as exc:
            raise CategorizedError(
                "could not launch ssh for live drgn script introspection",
                category=ErrorCategory.TRANSPORT_FAILURE,
            ) from exc

    proc = run_ssh_with_retry(run_once, policy=_LIVE_SSH_RETRY)
    _raise_on_live_ssh_failure(
        proc, "in-guest drgn script exited non-zero (script error or could not attach)"
    )
    return proc.stdout.decode("utf-8", "replace")


__all__ = [
    "IntrospectOutput",
    "LiveIntrospector",
    "LiveScriptOutput",
    "LocalLibvirtLiveIntrospect",
]
