"""Offline drgn introspection of a captured vmcore on the host (ADR-0033).

`LocalLibvirtVmcoreIntrospect` realizes the `VmcoreIntrospector` port, mirroring
`LocalLibvirtRetrieve`'s `CrashPostmortem`: fetching the raw core + `vmlinux` from the
object store, verifying the core's build-id against the Run's recorded build-id
(provenance), opening drgn against the staged core, and running three fixed helpers
(tasks, modules, sysinfo). `from_env` wires the real shared drgn seams (ADR-0210 §2); the
orchestration, provenance, dispatch, byte-cap, and redaction stay unit-tested with a fake
`_Program`, and the drgn open itself runs only under the `live_vm` gate. The assembled report
is `Redactor`-scrubbed **inside the port** — the port is the single redaction boundary, so any
later persistence is of already-redacted text. The real drgn package is an operator-provided
live-host prerequisite, not a normal service dependency: the open seam imports it lazily, so a
host without drgn surfaces a `MISSING_DEPENDENCY` from the open seam, not an ``ImportError``.
`LocalLibvirtLiveIntrospect` (the live drgn-over-SSH port, ADR-0219) SSH-execs the in-guest
`kdive-drgn <helper>` over the drgn-live SSH transport (ADR-0218) and assembles the same redacted
report — drgn runs in the guest, the worker only opens SSH and parses JSON, mirroring
`RemoteLibvirtLiveIntrospect`.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess  # noqa: S404 - fixed argv only, no shell; helper name validated before use
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.providers.ports.retrieve import (
    IntrospectOutput,
    LiveIntrospector,
    LiveScriptOutput,
    VmcoreIntrospector,
)
from kdive.providers.shared.debug_common.drgn_program import (
    open_vmcore_program,
    read_vmcoreinfo_build_id,
    run_introspection_helper,
)
from kdive.providers.shared.debug_common.introspect import (
    _REPORT_BYTE_CAP,
    _Program,
    assemble_report,
    assemble_script_output,
)
from kdive.security.secrets.secret_registry import SecretRegistry

# The fixed live-helper set (ADR-0033 §2 / ADR-0085): the same three in-tree helpers as the
# offline path. There is no caller-supplied drgn script — an unknown helper is rejected.
_LIVE_HELPERS = frozenset({"tasks", "modules", "sysinfo"})
# The single in-guest drgn helper the base image carries (ADR-0079/0085); SSH-exec'd with fixed
# argv against the guest's live /proc/kcore, prints one section JSON object on stdout.
_DRGN_HELPER = "/usr/local/sbin/kdive-drgn"
_LOOPBACK_HOST = "127.0.0.1"  # the live transport is loopback-only (ADR-0218 §1)
_SSH_USER = "root"  # the per-System bootstrap public key is injected to root (ADR-0289/0218 §1)
# Bound the SSH+helper round-trip. introspect.run runs the seam via asyncio.to_thread, so this
# also caps how long a wedged sshd can hold a worker thread-pool slot.
_LIVE_INTROSPECT_SSH_TIMEOUT_S = 60
_SSH_CONNECT_TIMEOUT_S = 10

# --- LocalLibvirtVmcoreIntrospect (the realized port) --------------------------------------

type _FetchObject = Callable[[str], bytes]
type _ReadBuildId = Callable[[bytes], str]
type _OpenProgram = Callable[[Path, Path], _Program]
type _RunHelper = Callable[[_Program, str], dict[str, object]]


class LocalLibvirtVmcoreIntrospect:
    """The realized offline-introspection port (ADR-0033).

    Stages the raw core + ``vmlinux`` from the object store, verifies the core's build-id
    against the Run's recorded build-id (provenance), opens drgn against the staged core
    (``live_vm`` seam), runs the three helpers, redacts and byte-caps the assembled report,
    and returns it — the port is the single redaction boundary.

    ``from_env`` wires the real ``open_program``/``run_helper`` seams; on a host without drgn the
    open seam raises ``MISSING_DEPENDENCY`` (it imports drgn lazily). A test may still pass ``None``
    seams to exercise the off-gate guard, which raises ``MISSING_DEPENDENCY`` before touching the
    store, mirroring ``LocalLibvirtRetrieve.run``'s seam guard.
    """

    def __init__(
        self,
        *,
        fetch_object: _FetchObject,
        read_vmcore_build_id: _ReadBuildId,
        secret_registry: SecretRegistry,
        open_program: _OpenProgram | None = None,
        run_helper: _RunHelper | None = None,
    ) -> None:
        self._fetch_object = fetch_object
        self._read_vmcore_build_id = read_vmcore_build_id
        self._secret_registry = secret_registry
        self._open_program = open_program
        self._run_helper = run_helper
        self._report_byte_cap = _REPORT_BYTE_CAP

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> LocalLibvirtVmcoreIntrospect:
        """Build from env with the real drgn seams (lazy: drgn imports on first use).

        drgn stays an operator-provided live-host prerequisite — the open seam imports it inside
        the call, so composition builds on hosts without it and ``from_vmcore`` raises the
        documented ``MISSING_DEPENDENCY`` from the open seam (not an up-front ``None`` guard).
        """
        # ``open_vmcore_program`` returns ``DrgnProgramAdapter`` (its ``iter_*`` are typed
        # ``list[object]``); cast it to the seam alias whose ``_Program`` reads the same surface
        # with the narrower helper-facing element types. ``run_introspection_helper`` accepts
        # ``Any`` for ``program`` so it needs no cast.
        return cls(
            fetch_object=_real_fetch_object,
            read_vmcore_build_id=read_vmcoreinfo_build_id,
            secret_registry=secret_registry,
            open_program=cast("_OpenProgram", open_vmcore_program),
            run_helper=run_introspection_helper,
        )

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        """Open the core, run the helpers, and return a redacted, size-bounded report.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` if the drgn seams were not configured
                (off-gate); ``CONFIGURATION_ERROR`` for a malformed ref rejected by an
                injected fetch/build-id seam or a build-id provenance mismatch;
                ``STALE_HANDLE`` when a referenced object is missing;
                ``INFRASTRUCTURE_FAILURE`` for object-store IO failures; or
                ``DEBUG_ATTACH_FAILURE`` if drgn cannot open the core or load the vmlinux.
        """
        if self._open_program is None or self._run_helper is None:
            raise CategorizedError(
                "offline drgn introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        vmcore_bytes = self._fetch_object(vmcore_ref)
        self._verify_provenance(vmcore_bytes, expected_build_id, vmcore_ref)
        vmlinux_bytes = self._fetch_object(debuginfo_ref)
        with (
            tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
            tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
        ):
            core_file.write(vmcore_bytes)
            core_file.flush()
            vmlinux_file.write(vmlinux_bytes)
            vmlinux_file.flush()
            program = self._open(self._open_program, Path(core_file.name), Path(vmlinux_file.name))
            tasks = self._run_helper(program, "tasks")
            modules = self._run_helper(program, "modules")
            sysinfo = self._run_helper(program, "sysinfo")
        return self._assemble(tasks, modules, sysinfo)

    def _verify_provenance(self, vmcore_bytes: bytes, expected: str, vmcore_ref: str) -> None:
        observed = self._read_vmcore_build_id(vmcore_bytes)
        if observed != expected:
            raise CategorizedError(
                "captured vmcore build-id does not match the Run's debuginfo build-id",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"vmcore_ref": vmcore_ref},
            )

    @staticmethod
    def _open(open_program: _OpenProgram, core: Path, vmlinux: Path) -> _Program:
        try:
            return open_program(core, vmlinux)
        except CategorizedError:
            raise
        except Exception as exc:  # noqa: BLE001 - any drgn open fault becomes a typed attach failure
            raise CategorizedError(
                "drgn could not open the vmcore against the supplied vmlinux",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            ) from exc

    def _assemble(
        self,
        tasks: dict[str, object],
        modules: dict[str, object],
        sysinfo: dict[str, object],
    ) -> IntrospectOutput:
        return assemble_report(
            tasks,
            modules,
            sysinfo,
            byte_cap=self._report_byte_cap,
            secret_registry=self._secret_registry,
        )


def _normalize_attach_error(exc: Exception, message: str) -> CategorizedError:
    """A categorized fault passes through; any other open fault becomes an attach failure."""
    if isinstance(exc, CategorizedError):
        return exc
    return CategorizedError(message, category=ErrorCategory.DEBUG_ATTACH_FAILURE)


# --- LocalLibvirtLiveIntrospect (the live drgn-over-SSH port, ADR-0219) ----------------------

# (transport_handle, helper, key_path) -> the section dict the in-guest ``kdive-drgn <helper>``
# emits. ``key_path`` is the caller-materialized per-System bootstrap private key (ADR-0289).
type _RunLiveHelper = Callable[[str, str, str], dict[str, object]]
# (transport_handle, script, timeout_sec, key_path) -> the in-guest ``kdive-drgn run-script``
# stdout.
type _RunLiveScript = Callable[[str, str, float, str], str]
# Seconds added to the agent-chosen in-guest timeout to bound the SSH round-trip (ADR-0240):
# a wedged sshd still releases the thread, but a legitimately long script is not severed.
_LIVE_SCRIPT_SSH_SLACK_S = 10.0


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
        """Build from env with the real SSH-exec seams (opens no SSH and imports no drgn here).

        The seams close over ``secret_registry`` only for defense-in-depth error-path redaction;
        the caller-supplied ``key_path`` (per-System bootstrap key, ADR-0289) is loaded and
        registered by the MCP tool boundary before it ever reaches this port. Their real
        ``subprocess`` SSH calls run only under the ``live_vm`` gate, but the handle-validation
        branches run in CI.
        """

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
        """SSH-exec a caller drgn script in-guest over the drgn-live transport; cap + redact stdout.

        The script is piped to the in-guest ``kdive-drgn run-script`` over SSH stdin (never argv);
        its stdout is redacted (platform secrets only) and byte-capped through
        ``assemble_script_output``. drgn runs **in the guest**; the worker only opens SSH.
        ``key_path`` is the per-System bootstrap private key (ADR-0289), already loaded and
        materialized to a ``0600`` temp file by the caller (the MCP tool boundary), which also
        registered its content with a ``SecretRegistry`` for redaction.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` if the live seam was not configured (off-gate);
                a transport-layer ``CategorizedError`` (``transport_failure`` /
                ``debug_attach_failure`` / ``configuration_error``) propagated from the seam;
                ``DEBUG_ATTACH_FAILURE`` if the seam fails for any other reason.
        """
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
        """SSH-exec one in-guest helper over the drgn-live transport; return a redacted report.

        Validates ``helper`` against the fixed set **before** the seam runs (no SSH round-trip for
        a bad helper), routes the returned section into its report field, and redacts + byte-caps
        through ``assemble_report``. ``key_path`` is the per-System bootstrap private key
        (ADR-0289), already loaded and materialized to a ``0600`` temp file by the caller (the MCP
        tool boundary), which also registered its content with a ``SecretRegistry`` for redaction.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` if the live seam was not configured (off-gate);
                ``CONFIGURATION_ERROR`` if ``helper`` is not one of the fixed in-tree helper names;
                a transport-layer ``CategorizedError`` (``transport_failure`` /
                ``debug_attach_failure`` / ``infrastructure_failure`` / ``configuration_error``)
                propagated from the seam;
                ``DEBUG_ATTACH_FAILURE`` if the seam fails for any other reason.
        """
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
    """Decode the handle and return the loopback SSH port; raise CONFIGURATION_ERROR otherwise.

    Re-enforces the ``ssh`` scheme + loopback host at use time (defense-in-depth: the connect
    plane already enforced loopback at open time, ADR-0218 §1), so a tampered or forged handle
    cannot redirect the SSH connection off loopback. Runs before any IO.
    """
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
    """Build the fixed loopback SSH argv for an in-guest ``kdive-drgn`` invocation.

    ``key_path`` is the caller-materialized per-System bootstrap private key (ADR-0289); its
    content was already loaded and registered with ``secret_registry`` by the MCP tool boundary
    before this call, so this seam does no key IO or registration of its own — ``secret_registry``
    is accepted only so the type stays consistent with the rest of the module's seams.
    """
    port = _validate_ssh_target(transport_handle)
    del secret_registry  # unused: key content is registered by the caller (the tool boundary)
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
    """SSH-exec ``kdive-drgn <helper>`` in the guest and return its section dict (ADR-0219).

    Decodes + re-validates the handle (loopback ssh, before IO) and runs ``ssh … kdive-drgn
    <helper>`` with fixed argv using the caller-supplied ``key_path`` (the per-System bootstrap
    key, ADR-0289, already materialized by the MCP tool boundary) as the ``root`` identity. The
    helper name is validated by the caller against the fixed set, so no caller-controlled string
    reaches the remote command.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a non-ssh/non-loopback handle (before IO);
            ``TRANSPORT_FAILURE`` for an SSH launch/connect fault or timeout;
            ``DEBUG_ATTACH_FAILURE`` for a non-zero helper exit (drgn could not attach in-guest);
            ``INFRASTRUCTURE_FAILURE`` for undecodable / non-object helper stdout.
    """
    argv = _live_ssh_argv(transport_handle, secret_registry, [helper], key_path)
    return _exec_live_helper(argv)


def _exec_live_helper(argv: list[str]) -> dict[str, object]:  # pragma: no cover - live_vm
    """Run the fixed ssh argv and decode the in-guest helper's one JSON section object.

    The ``# pragma: no cover - live_vm`` covers the real ssh subprocess; it needs a booted guest
    with a reachable loopback-forwarded sshd, the per-System bootstrap key authorized, and the
    in-guest ``kdive-drgn`` + ``drgn`` (the ADR-0219 named live gaps).
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; helper pre-validated
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
    if proc.returncode != 0:
        raise CategorizedError(
            "in-guest drgn helper exited non-zero (could not attach to the live kernel)",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"exit_status": proc.returncode},
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
    """SSH-exec ``kdive-drgn run-script <timeout>`` with the script on stdin (ADR-0240).

    Decodes + re-validates the handle (loopback ssh, before IO) and runs ``ssh … kdive-drgn
    run-script <timeout>`` with the caller script piped over stdin — never argv — using the
    caller-supplied ``key_path`` (the per-System bootstrap key, ADR-0289, already materialized by
    the MCP tool boundary). Returns the script's raw stdout.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a non-ssh/non-loopback handle (before IO);
            ``TRANSPORT_FAILURE`` for an SSH launch/connect fault or timeout;
            ``DEBUG_ATTACH_FAILURE`` for a non-zero in-guest exit (script error or drgn could not
            attach).
    """
    # Re-assert the in-guest timeout floor at the argv boundary (defense in depth): coreutils
    # `timeout 0` disables the bound, so the in-guest value is always >= 1 regardless of caller.
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
    """Run the fixed ssh argv with the caller script on stdin; return its raw stdout.

    The ``# pragma: no cover - live_vm`` covers the real ssh subprocess (a booted guest with a
    reachable loopback-forwarded sshd, the per-System bootstrap key authorized, and in-guest
    ``kdive-drgn`` + ``drgn``). The in-guest ``timeout`` bounds drgn; ``ssh_timeout_s`` (the
    in-guest bound + slack) bounds a wedged channel so the worker thread is always released.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; script via stdin only
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
    if proc.returncode != 0:
        raise CategorizedError(
            "in-guest drgn script exited non-zero (script error or could not attach)",
            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            details={"exit_status": proc.returncode},
        )
    return proc.stdout.decode("utf-8", "replace")


def _real_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    from kdive.store.objectstore import object_store_from_env

    # The ref is a key the system itself produced; there is no client etag handle, so the
    # read is unconditional (ADR-0054). An empty etag would 412 here, not skip the check.
    return object_store_from_env().get_artifact(ref, None).data


__all__ = [
    "IntrospectOutput",
    "LiveIntrospector",
    "LocalLibvirtLiveIntrospect",
    "LocalLibvirtVmcoreIntrospect",
    "VmcoreIntrospector",
]
