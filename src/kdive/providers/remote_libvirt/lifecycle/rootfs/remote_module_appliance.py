"""Confined transient appliance supervision for remote module recovery (ADR-0585)."""

from __future__ import annotations

import codecs
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

import libvirt

from kdive.domain.errors import CategorizedError
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
    ExpectedAppliance,
    ExpectedAttachmentState,
    normalized_appliance_devices,
    validate_appliance_xml,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleResultV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import PreparedVolume
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

CONSOLE_TAIL_BYTES = 16 * 1024
WAIT_TIMEOUT_SECONDS = 30.0
INVOCATION_TIMEOUT_SECONDS = 5 * 60.0
_PHASES = {
    "accepted",
    "captured",
    "staging-intent",
    "replacement-ready",
    "installed",
    "restore-ready",
    "restored",
}
_ERROR_CODES = {
    "INVALID_DOCUMENT",
    "IDENTITY_MISMATCH",
    "LIMIT_EXCEEDED",
    "SOURCE_INVALID",
    "ROOT_DISCOVERY_FAILED",
    "FILESYSTEM_FAILURE",
    "RECOVERY_CONFLICT",
    "DEPMOD_FAILURE",
    "FLUSH_FAILURE",
    "SHUTDOWN_FAILURE",
}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]{0,127}\Z")


class ApplianceStream(Protocol):
    def recv(self, size: int) -> bytes | int | None: ...
    def abort(self) -> int: ...


class ApplianceDomain(Protocol):
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802
    def isActive(self) -> int: ...  # noqa: N802
    def isPersistent(self) -> int: ...  # noqa: N802
    def openConsole(  # noqa: N802
        self, device: str | None, stream: ApplianceStream, flags: int
    ) -> int: ...
    def destroy(self) -> int: ...


class ApplianceConn(Protocol):
    def lookupByName(self, name: str) -> ApplianceDomain: ...  # noqa: N802
    def createXML(self, xml: str, flags: int = 0) -> ApplianceDomain: ...  # noqa: N802
    def newStream(self, flags: int = 0) -> ApplianceStream: ...  # noqa: N802


_T = TypeVar("_T")


class DeadlineExecutor(Protocol):
    """Run a potentially blocking call no later than one monotonic deadline."""

    def call(self, operation: Callable[[], _T], deadline: float) -> _T: ...


class UnresolvedCallError(TimeoutError):
    """A deadline expired while the underlying binding call may still be running."""


@dataclass(frozen=True, slots=True)
class ApplianceRequest:
    name: str
    architecture: str
    emulator_path: str
    memory_kib: int
    vcpus: int
    pool: str
    appliance_volume: str
    appliance_image_digest: str
    root: PreparedVolume
    source: PreparedVolume
    scratch: PreparedVolume
    operation: RemoteModuleOperationV1
    secret_registry: SecretRegistry
    read_scratch_result: Callable[[], bytes | None]
    inspect_attachments: Callable[[], AttachmentInspection]
    executor: DeadlineExecutor
    monotonic: Callable[[], float]
    invocation_deadline: float | None = None

    def __post_init__(self) -> None:
        if self.architecture not in {"x86_64", "ppc64le"}:
            raise ValueError("unsupported remote module appliance architecture")
        if not self.emulator_path.startswith("/"):
            raise ValueError("appliance emulator path must be absolute")
        if self.operation.appliance_image_digest != self.appliance_image_digest:
            raise ValueError("appliance image digest does not match operation")
        expected = (self.root, self.source, self.scratch)
        if any(volume.pool != self.pool for volume in expected):
            raise ValueError("appliance volumes must share one pool")


@dataclass(frozen=True, slots=True)
class ApplianceOutcome:
    result: RemoteModuleResultV1 | None
    console_tail: str
    timed_out: bool


@dataclass(frozen=True, slots=True)
class TeardownObservation:
    absent: bool
    root_detached: bool
    source_detached: bool
    scratch_detached: bool

    @property
    def complete(self) -> bool:
        return self.absent and self.root_detached and self.source_detached and self.scratch_detached


def _disk(parent: ET.Element, pool: str, volume: str, alias: str, *, readonly: bool) -> None:
    disk = ET.SubElement(parent, "disk", {"type": "volume", "device": "disk"})
    ET.SubElement(disk, "source", {"pool": pool, "volume": volume})
    ET.SubElement(disk, "target", {"dev": alias, "bus": "virtio"})
    if readonly:
        ET.SubElement(disk, "readonly")


def render_remote_module_appliance(request: ApplianceRequest) -> str:
    """Render the closed, network-free transient domain definition."""
    domain = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(domain, "name").text = request.name
    ET.SubElement(domain, "memory", {"unit": "KiB"}).text = str(request.memory_kib)
    ET.SubElement(domain, "vcpu").text = str(request.vcpus)
    os_node = ET.SubElement(domain, "os")
    machine = "q35" if request.architecture == "x86_64" else "pseries"
    ET.SubElement(os_node, "type", {"arch": request.architecture, "machine": machine}).text = "hvm"
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(
        metadata,
        "remote-module-appliance",
        {
            "system": request.operation.system_id,
            "image-digest": request.appliance_image_digest,
            "nonce": request.operation.operation_nonce,
        },
    )
    devices = ET.SubElement(domain, "devices")
    _disk(devices, request.pool, request.appliance_volume, "vda", readonly=True)
    _disk(devices, request.pool, request.root.name, "vdb", readonly=False)
    _disk(devices, request.pool, request.source.name, "vdc", readonly=True)
    _disk(devices, request.pool, request.scratch.name, "vdd", readonly=False)
    for tag, attributes in normalized_appliance_devices(request.architecture):
        ET.SubElement(devices, tag, attributes)
    ET.SubElement(devices, "emulator").text = request.emulator_path
    ET.SubElement(devices, "console", {"type": "pty"})
    return ET.tostring(domain, encoding="unicode", short_empty_elements=True)


def expected_attachment_state(request: ApplianceRequest) -> ExpectedAttachmentState:
    return ExpectedAttachmentState(
        request.operation.system_id,
        request.pool,
        request.root.name,
        request.source.name,
        request.scratch.name,
        ExpectedAppliance(
            request.name,
            request.architecture,
            request.appliance_image_digest,
            request.operation.operation_nonce,
            request.appliance_volume,
            "q35" if request.architecture == "x86_64" else "pseries",
            request.memory_kib,
            request.vcpus,
            request.emulator_path,
        ),
    )


def _call_mutation[T](request: ApplianceRequest, deadline: float, operation: Callable[[], T]) -> T:
    def admitted() -> T:
        if request.monotonic() >= deadline:
            raise TimeoutError
        return operation()

    return request.executor.call(admitted, deadline)


def _domain(
    conn: ApplianceConn, request: ApplianceRequest, wanted: str, deadline: float
) -> ApplianceDomain:
    try:
        domain = request.executor.call(lambda: conn.lookupByName(request.name), deadline)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
            raise
        return _call_mutation(
            request,
            deadline,
            lambda: conn.createXML(wanted, libvirt.VIR_DOMAIN_START_AUTODESTROY),
        )
    try:
        active = request.executor.call(domain.isActive, deadline)
        persistent = request.executor.call(domain.isPersistent, deadline)
        if not active or persistent:
            raise RuntimeError("existing appliance is not active and transient")
        actual = request.executor.call(lambda: domain.XMLDesc(0), deadline)
        validate_appliance_xml(actual, expected_attachment_state(request))
    except CategorizedError:
        raise
    except (libvirt.libvirtError, ET.ParseError) as exc:
        raise RuntimeError("could not verify appliance identity") from exc
    return domain


def _read_console(
    conn: ApplianceConn,
    domain: ApplianceDomain,
    request: ApplianceRequest,
    deadline: float,
    invocation_deadline: float,
) -> tuple[str, bool, bool]:
    stream = request.executor.call(lambda: conn.newStream(libvirt.VIR_STREAM_NONBLOCK), deadline)
    console_flags = libvirt.VIR_DOMAIN_CONSOLE_FORCE | libvirt.VIR_DOMAIN_CONSOLE_SAFE
    try:
        request.executor.call(lambda: domain.openConsole(None, stream, console_flags), deadline)
    except UnresolvedCallError:
        # The open may still be executing. Do not race it with an abort; the
        # transient domain and its durable scratch state remain retryable.
        raise
    except Exception:
        try:
            request.executor.call(stream.abort, deadline)
        except UnresolvedCallError:
            raise
        except libvirt.libvirtError, TimeoutError:
            pass
        raise
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = ""
    timed_out = False
    unresolved_rpc = False
    try:
        while request.monotonic() < deadline:
            chunk = request.executor.call(lambda: stream.recv(4096), deadline)
            if chunk == -2:
                remaining = deadline - request.monotonic()
                if remaining > 0:
                    time.sleep(min(0.01, remaining))
                continue
            if chunk is None or chunk == 0 or chunk == b"":
                break
            if not isinstance(chunk, bytes):
                raise RuntimeError("invalid appliance console stream result")
            deadline = min(
                invocation_deadline,
                request.monotonic() + WAIT_TIMEOUT_SECONDS,
            )
            text += decoder.decode(chunk)
            text = Redactor(registry=request.secret_registry).redact_text(text)
            # Preserve enough prefix for a secret or key/value token split across reads.
            carry = CONSOLE_TAIL_BYTES + max(
                (len(value) for value in request.secret_registry.snapshot()), default=0
            )
            text = text[-carry:]
        else:
            timed_out = True
    except UnresolvedCallError:
        timed_out = True
        unresolved_rpc = True
    except TimeoutError:
        timed_out = True
    except libvirt.libvirtError:
        timed_out = True
    finally:
        # Unconditional: libvirt frees a stream when it is finished or aborted
        # or when the connection closes, not when the Python wrapper is
        # collected, so a worker holding one connection across many invocations
        # would leak a stream per successful appliance run.
        if not unresolved_rpc:
            cancellation_deadline = min(
                invocation_deadline,
                request.monotonic() + WAIT_TIMEOUT_SECONDS,
            )
            try:
                request.executor.call(stream.abort, cancellation_deadline)
            except UnresolvedCallError:
                unresolved_rpc = True
            except libvirt.libvirtError, TimeoutError:
                pass
    text += decoder.decode(b"", final=True)
    text = Redactor(registry=request.secret_registry).redact_text(text)
    text = _filter_console(text)
    encoded = text.encode("utf-8")
    if len(encoded) > CONSOLE_TAIL_BYTES:
        retained: list[str] = []
        retained_bytes = 0
        for line in reversed(text.splitlines(keepends=True)):
            line_bytes = len(line.encode("utf-8"))
            if retained_bytes + line_bytes > CONSOLE_TAIL_BYTES:
                break
            retained.append(line)
            retained_bytes += line_bytes
        text = "".join(reversed(retained))
    return text, timed_out, unresolved_rpc


def _filter_console(text: str) -> str:
    """Retain only stable, validated protocol fields from the redacted console."""
    validators: dict[str, Callable[[str], bool]] = {
        "phase": lambda value: value in _PHASES,
        "error_code": lambda value: value in _ERROR_CODES,
        "system_id": lambda value: _UUID.fullmatch(value) is not None,
        "run_id": lambda value: _UUID.fullmatch(value) is not None,
        "plan_identity": lambda value: _DIGEST.fullmatch(value) is not None,
        "operation_nonce": lambda value: _NONCE.fullmatch(value) is not None,
        "appliance_image_digest": lambda value: _DIGEST.fullmatch(value) is not None,
        "root_volume_key": lambda value: (
            bool(value)
            and len(value) <= 255
            and value.isprintable()
            and not value.startswith(("/", "\\"))
            and ".." not in value.split("/")
            and not any(character in value for character in ("\0", ":", "@"))
        ),
        "root_volume_identity": lambda value: _DIGEST.fullmatch(value) is not None,
        "source_manifest": lambda value: _DIGEST.fullmatch(value) is not None,
        "installed_manifest": lambda value: _DIGEST.fullmatch(value) is not None,
        "capture_manifest": lambda value: _DIGEST.fullmatch(value) is not None,
        "release": lambda value: _RELEASE.fullmatch(value) is not None,
        "entry_count": lambda value: (
            value.isascii() and value.isdigit() and len(value) <= 6 and int(value) <= 200_000
        ),
        "content_bytes": lambda value: (
            value.isascii() and value.isdigit() and len(value) <= 10 and int(value) <= 8 * 1024**3
        ),
    }
    retained: list[str] = []
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        validator = validators.get(key)
        if separator and validator is not None and validator(value):
            retained.append(line)
    return "".join(f"{line}\n" for line in retained)


def _wait_deadline(request: ApplianceRequest, invocation_deadline: float) -> float:
    return min(invocation_deadline, request.monotonic() + WAIT_TIMEOUT_SECONDS)


def _attempt_timeout_teardown(
    domain: ApplianceDomain,
    request: ApplianceRequest,
    invocation_deadline: float,
) -> None:
    """Attempt bounded teardown after a resolved wait expiry without claiming success."""
    try:
        deadline = _wait_deadline(request, invocation_deadline)
        _call_mutation(request, deadline, domain.destroy)
    except UnresolvedCallError:
        return
    except Exception:
        pass
    try:
        inspection = request.executor.call(
            request.inspect_attachments,
            _wait_deadline(request, invocation_deadline),
        )
        observe_appliance_absent_and_detached(
            expected_attachment_state(request), lambda: inspection
        )
    except Exception:
        return


def run_or_adopt_appliance(conn: ApplianceConn, request: ApplianceRequest) -> ApplianceOutcome:
    """Launch or adopt an identical appliance and return only durable result evidence."""
    started = request.monotonic()
    invocation_deadline = min(
        request.invocation_deadline or started + INVOCATION_TIMEOUT_SECONDS,
        started + INVOCATION_TIMEOUT_SECONDS,
    )
    wait_deadline = min(invocation_deadline, request.monotonic() + WAIT_TIMEOUT_SECONDS)
    try:
        domain = _domain(conn, request, render_remote_module_appliance(request), wait_deadline)
    except TimeoutError:
        return ApplianceOutcome(None, "", True)
    console_tail = ""
    try:
        console_tail, timed_out, unresolved_rpc = _read_console(
            conn,
            domain,
            request,
            _wait_deadline(request, invocation_deadline),
            invocation_deadline,
        )
        if unresolved_rpc:
            return ApplianceOutcome(None, console_tail, True)
        if timed_out:
            if not unresolved_rpc:
                _attempt_timeout_teardown(domain, request, invocation_deadline)
            return ApplianceOutcome(None, console_tail, True)
        raw_result = request.executor.call(
            request.read_scratch_result,
            _wait_deadline(request, invocation_deadline),
        )
        result = None
        if raw_result is not None:
            result = RemoteModuleResultV1.from_canonical_json(raw_result)
            result.validate_for(request.operation)
            if result.status == "success":
                teardown = request.executor.call(
                    request.inspect_attachments,
                    _wait_deadline(request, invocation_deadline),
                )
                observation = observe_appliance_absent_and_detached(
                    expected_attachment_state(request), lambda: teardown
                )
                if not observation.complete:
                    result = None
        return ApplianceOutcome(result, console_tail, False)
    except UnresolvedCallError:
        return ApplianceOutcome(None, console_tail, True)
    except TimeoutError:
        _attempt_timeout_teardown(domain, request, invocation_deadline)
        return ApplianceOutcome(None, console_tail, True)
    except Exception:
        _attempt_timeout_teardown(domain, request, invocation_deadline)
        raise


def observe_appliance_absent_and_detached(
    expected: ExpectedAttachmentState,
    inspect: Callable[[], AttachmentInspection],
) -> TeardownObservation:
    """Translate one completed attachment inspection into terminal teardown proof."""
    observation = inspect()
    absent = not observation.appliance_present
    root_detached = absent and observation.system_shut_off and observation.exclusive
    return TeardownObservation(
        absent,
        root_detached,
        observation.proves_detached(expected.pool, expected.source_volume),
        observation.proves_detached(expected.pool, expected.scratch_volume),
    )


def teardown_remote_module_appliance(
    conn: ApplianceConn, request: ApplianceRequest
) -> TeardownObservation:
    """Destroy only an identity-matching transient appliance, then prove detachment."""
    deadline = request.invocation_deadline or request.monotonic() + INVOCATION_TIMEOUT_SECONDS
    expected = expected_attachment_state(request)
    try:
        domain = request.executor.call(lambda: conn.lookupByName(request.name), deadline)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
            raise
    else:
        actual = request.executor.call(lambda: domain.XMLDesc(0), deadline)
        validate_appliance_xml(actual, expected)
        _call_mutation(request, deadline, domain.destroy)
    inspection = request.executor.call(request.inspect_attachments, deadline)
    return observe_appliance_absent_and_detached(expected, lambda: inspection)
