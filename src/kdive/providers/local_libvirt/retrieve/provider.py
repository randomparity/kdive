"""Local-libvirt Retrieve plane: vmcore capture and crash postmortem (ADR-0031).

The crash-command validator is the port-boundary security control: caller commands are
sanitized and allowlist-checked before any `crash` invocation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

from kdive.artifacts.storage import (
    ArtifactStreamRequest,
    ArtifactWriteRequest,
    HeadResult,
    StoredArtifact,
)
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve.guestfs import (
    _real_wait_for_vmcore,
    _remove_spool,
)
from kdive.providers.local_libvirt.retrieve.host_dump_capture import _real_host_dump_capture
from kdive.providers.local_libvirt.retrieve.kdump import (
    HarvestOutcome,
    file_sha256_b64,
    read_via_tempfile,
    redact_dmesg,
)
from kdive.providers.ports.retrieve import (
    CaptureOutput,
    CrashOutput,
    CrashResult,
)
from kdive.providers.shared.debug_common.core_file import (
    read_core_build_id_from_file,
    read_core_dmesg_from_file,
)
from kdive.providers.shared.debug_common.crash_postmortem import (
    _real_run_crash,
)
from kdive.providers.shared.debug_common.crash_postmortem import (
    run_crash_postmortem as _run_crash_postmortem,
)
from kdive.providers.shared.runtime_paths import (
    WORKER_READABILITY_REMEDIATION,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import UNCONFIGURED_OBJECT_STORE
from kdive.store.objectstore import ObjectStore

_log = logging.getLogger(__name__)

_RETENTION_CLASS = "vmcore"

# Cause-neutral operator guidance for an incomplete kdump core (ADR-0251). kdump writes
# ``/var/crash/<ts>/vmcore-incomplete`` while saving and renames it to ``vmcore`` only on success,
# so an ``-incomplete`` file that survives means the save never finished. The two field causes are
# an in-guest ``makedumpfile`` older than the kernel under test, or a capture that ran past the
# window — this guidance does not assert which, and interpolates no guest output.
KDUMP_CORE_INCOMPLETE_REMEDIATION = (
    "kdump left an incomplete core (vmcore-incomplete) and never finished a complete vmcore; "
    "common causes are an in-guest makedumpfile older than the kernel under test, or a capture "
    'that exceeded the window. Retry with method="host_dump", or use a rootfs image whose '
    "makedumpfile supports this kernel (e.g. fedora-kdive-ready-44)"
)


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...
    def put_stream(self, request: ArtifactStreamRequest) -> StoredArtifact: ...
    def head(self, key: str) -> HeadResult | None: ...


type _WaitForVmcore = Callable[[UUID], HarvestOutcome]
type _HostDumpCapture = Callable[[UUID], Path | None]
type _ReadBuildId = Callable[[bytes], str]
type _ReadBuildIdFromFile = Callable[[Path], str]
type _ExtractRedactedFromFile = Callable[[Path], bytes]
type _FetchObject = Callable[[str], bytes]
type _FetchVersionedObject = Callable[[str, str], bytes]
type _RunCrash = Callable[[Path, Path, str], CrashResult]


class LocalLibvirtRetrieve:
    """The realized Retrieve port: kdump capture + crash postmortem (ADR-0031)."""

    def __init__(
        self,
        *,
        tenant: str,
        store_factory: Callable[[], _StorePort],
        wait_for_vmcore: _WaitForVmcore,
        read_vmcore_build_id: _ReadBuildId,
        read_vmcore_build_id_from_file: _ReadBuildIdFromFile,
        extract_redacted_from_file: _ExtractRedactedFromFile,
        host_dump_capture: _HostDumpCapture,
        secret_registry: SecretRegistry,
        fetch_object: _FetchObject | None = None,
        fetch_versioned_object: _FetchVersionedObject | None = None,
        run_crash: _RunCrash | None = None,
    ) -> None:
        self._tenant = tenant
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._wait_for_vmcore = wait_for_vmcore
        self._read_vmcore_build_id = read_vmcore_build_id
        self._read_vmcore_build_id_from_file = read_vmcore_build_id_from_file
        self._extract_redacted_from_file = extract_redacted_from_file
        self._host_dump_capture = host_dump_capture
        self._fetch_object = fetch_object
        self._fetch_versioned_object = fetch_versioned_object
        self._run_crash = run_crash
        self._secret_registry = secret_registry

    @classmethod
    def from_env(
        cls, *, secret_registry: SecretRegistry, store: ObjectStore = UNCONFIGURED_OBJECT_STORE
    ) -> LocalLibvirtRetrieve:
        """Build from env; does not poll the host, open S3, or spawn `crash` (lazy seams)."""
        return cls(
            tenant="local",
            store_factory=lambda: store,
            wait_for_vmcore=_real_wait_for_vmcore,
            read_vmcore_build_id=_real_read_build_id,
            read_vmcore_build_id_from_file=read_core_build_id_from_file,
            extract_redacted_from_file=lambda core: redact_dmesg(
                core, read_core_dmesg_from_file, secret_registry
            ),
            host_dump_capture=_real_host_dump_capture,
            fetch_object=lambda ref: store.get_artifact(ref, None).data,
            fetch_versioned_object=lambda ref, version_id: (
                store.get_artifact(ref, None, version_id=version_id).data
            ),
            run_crash=_real_run_crash,
            secret_registry=secret_registry,
        )

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Capture a Run-owned core plus redacted dmesg, returning refs and build-id."""
        if method is CaptureMethod.HOST_DUMP:
            return self._capture_via_file(
                system_id, run_id, method, self._host_dump_capture(system_id)
            )
        outcome = self._wait_for_vmcore(system_id)
        if outcome.core is None and outcome.incomplete_found:
            raise self._incomplete_core(system_id)
        return self._capture_via_file(system_id, run_id, method, outcome.core)

    def _capture_via_file(
        self, system_id: UUID, run_id: UUID, method: CaptureMethod, core: Path | None
    ) -> CaptureOutput:
        """Store a captured core without whole-core buffering, then remove its spool dir."""
        if core is None:
            raise self._no_core(system_id)
        try:
            build_id = self._read_vmcore_build_id_from_file(core)
            raw = self._put_stream(run_id, f"vmcore-{method.value}", core)
            redacted = self._put(
                run_id,
                f"vmcore-{method.value}-redacted",
                self._extract_redacted_from_file(core),
                Sensitivity.REDACTED,
            )
            return CaptureOutput(
                raw=raw,
                redacted=redacted,
                vmcore_build_id=build_id,
                raw_size_bytes=core.stat().st_size,
            )
        except PermissionError as err:
            # A host_dump core is written by the QEMU/root process under qemu:///system, so a
            # non-root worker cannot read it back (ADR-0223). This is a host config problem that
            # never heals on retry, not the uncategorized infrastructure failure it surfaced as.
            raise CategorizedError(
                "failed to read the captured core",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "system_id": str(system_id),
                    "operation": "read_spooled_core",
                    "error": type(err).__name__,
                    "remediation": WORKER_READABILITY_REMEDIATION,
                },
            ) from err
        finally:
            _remove_spool(core)

    @staticmethod
    def _no_core(system_id: UUID) -> CategorizedError:
        return CategorizedError(
            "no complete core appeared within the capture window",
            category=ErrorCategory.READINESS_FAILURE,
            details={"system_id": str(system_id)},
        )

    @staticmethod
    def _incomplete_core(system_id: UUID) -> CategorizedError:
        """An incomplete ``vmcore-incomplete`` was harvested but no complete ``vmcore`` (ADR-0251).

        Distinct from ``_no_core`` (a genuinely empty ``/var/crash``): here kdump produced a
        partial core, so the readiness failure carries the cause-neutral
        ``KDUMP_CORE_INCOMPLETE_REMEDIATION`` and a ``kdump_core_incomplete`` reason a caller can
        branch on.
        """
        return CategorizedError(
            "kdump left an incomplete core; no complete vmcore was captured",
            category=ErrorCategory.READINESS_FAILURE,
            details={
                "reason": "kdump_core_incomplete",
                "remediation": KDUMP_CORE_INCOMPLETE_REMEDIATION,
                "system_id": str(system_id),
            },
        )

    def _put_stream(self, run_id: UUID, name: str, core: Path) -> StoredArtifact:
        """Stream ``core`` to the object store (Run-owned) and verify the checksum (ADR-0094)."""
        sha256_b64 = file_sha256_b64(core)
        store = self._ensure_store()
        stored = store.put_stream(
            ArtifactStreamRequest(
                tenant=self._tenant,
                owner_kind="runs",
                owner_id=str(run_id),
                name=name,
                path=core,
                sha256_b64=sha256_b64,
                sensitivity=Sensitivity.SENSITIVE,
                retention_class=_RETENTION_CLASS,
            )
        )
        self._verify_stored(stored.key, sha256_b64, run_id)
        return stored

    def _verify_stored(self, key: str, sha256_b64: str, run_id: UUID) -> None:
        head = self._ensure_store().head(key)
        if head is None:
            raise CategorizedError(
                "stored kdump core is absent after a success-reporting put",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"run_id": str(run_id), "key": key},
            )
        if head.checksum_sha256 is not None and head.checksum_sha256 != sha256_b64:
            raise CategorizedError(
                "stored kdump core checksum does not match the streamed core",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"run_id": str(run_id), "key": key},
            )

    def _ensure_store(self) -> _StorePort:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    def _put(self, run_id: UUID, name: str, data: bytes, sens: Sensitivity) -> StoredArtifact:
        return self._ensure_store().put_artifact(
            ArtifactWriteRequest(
                tenant=self._tenant,
                owner_kind="runs",
                owner_id=str(run_id),
                name=name,
                data=data,
                sensitivity=sens,
                retention_class=_RETENTION_CLASS,
            )
        )

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        debuginfo_version_id: str | None = None,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        """Symbolize the core against ``debuginfo_ref`` and run the crash command batch.

        Delegates to the provider-neutral worker-side helper (ADR-0084); raises the same
        categories.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a rejected crash command,
                malformed ref rejected by an injected fetch/build-id seam, or a build-id
                provenance mismatch;
                ``MISSING_DEPENDENCY`` if the crash seams were not configured;
                ``STALE_HANDLE`` when a referenced object is missing; or
                ``INFRASTRUCTURE_FAILURE`` for object-store IO failures.
        """
        if self._fetch_object is None or self._run_crash is None:
            raise CategorizedError(
                "crash seams not configured on this Retriever",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        return _run_crash_postmortem(
            vmcore_ref=vmcore_ref,
            debuginfo_ref=debuginfo_ref,
            debuginfo_version_id=debuginfo_version_id,
            expected_build_id=expected_build_id,
            commands=commands,
            fetch_object=self._fetch_object,
            fetch_versioned_object=self._fetch_versioned_object,
            read_build_id=self._read_vmcore_build_id,
            run_crash=self._run_crash,
            secret_registry=self._secret_registry,
        )


def _real_read_build_id(data: bytes) -> str:  # pragma: no cover - live_vm (drgn)
    return read_via_tempfile(data, read_core_build_id_from_file)


__all__ = [
    "LocalLibvirtRetrieve",
]
