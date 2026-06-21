"""Local-libvirt Retrieve plane: capture a kdump vmcore and run crash postmortem (ADR-0031).

`LocalLibvirtRetrieve` realizes two seam-injected ports, mirroring `LocalLibvirtBuild`:
`Retriever.capture(system_id, method)` dispatches to the appropriate seam, stores the raw
`sensitive` core and a `redacted` dmesg derivative, and returns both refs plus the core's build-id;
`CrashPostmortem.run_crash_postmortem(...)` symbolizes the core against the Run's
`debuginfo_ref` over an injected `crash` subprocess. The slow, host-bound operations are
`live_vm`-gated seams, so the orchestration and the full error contract are unit-tested with
fakes. The crash-command
validator is the load-bearing security control at the port boundary: every caller command is
sanitized and allowlist-checked before any `crash` invocation.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt

import kdive.config as config
from kdive.artifacts.storage import (
    ArtifactStreamRequest,
    ArtifactWriteRequest,
    HeadResult,
    StoredArtifact,
)
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.storage import overlay_path
from kdive.providers.local_libvirt.retrieve_kdump import (
    VmcoreEntry,
    file_sha256_b64,
    harvest_vmcore,
    read_via_tempfile,
    redact_dmesg,
)
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.ports import (
    CaptureOutput,
    CrashOutput,
    CrashResult,
)
from kdive.providers.shared.debug_common.core_file import (
    MAX_CORE_BYTES,
    read_core_build_id_from_file,
    read_core_dmesg_from_file,
)
from kdive.providers.shared.debug_common.crash_postmortem import (
    default_fetch_object,
    default_run_crash,
)
from kdive.providers.shared.debug_common.crash_postmortem import (
    run_crash_postmortem as _run_crash_postmortem,
)
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)

_RETENTION_CLASS = "vmcore"


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...
    def put_stream(self, request: ArtifactStreamRequest) -> StoredArtifact: ...
    def head(self, key: str) -> HeadResult | None: ...


type _WaitForVmcore = Callable[[UUID], Path | None]
type _HostDumpCapture = Callable[[UUID], bytes | None]
type _ReadBuildId = Callable[[bytes], str]
type _ReadBuildIdFromFile = Callable[[Path], str]
type _ExtractRedacted = Callable[[bytes], bytes]
type _ExtractRedactedFromFile = Callable[[Path], bytes]
type _FetchObject = Callable[[str], bytes]
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
        extract_redacted: _ExtractRedacted,
        extract_redacted_from_file: _ExtractRedactedFromFile,
        host_dump_capture: _HostDumpCapture,
        secret_registry: SecretRegistry,
        fetch_object: _FetchObject | None = None,
        run_crash: _RunCrash | None = None,
    ) -> None:
        self._tenant = tenant
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._wait_for_vmcore = wait_for_vmcore
        self._read_vmcore_build_id = read_vmcore_build_id
        self._read_vmcore_build_id_from_file = read_vmcore_build_id_from_file
        self._extract_redacted = extract_redacted
        self._extract_redacted_from_file = extract_redacted_from_file
        self._host_dump_capture = host_dump_capture
        self._fetch_object = fetch_object
        self._run_crash = run_crash
        self._secret_registry = secret_registry

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> LocalLibvirtRetrieve:
        """Build from env; does not poll the host, open S3, or spawn `crash` (lazy seams)."""
        return cls(
            tenant="local",
            store_factory=object_store_from_env,
            wait_for_vmcore=_real_wait_for_vmcore,
            read_vmcore_build_id=_real_read_build_id,
            read_vmcore_build_id_from_file=read_core_build_id_from_file,
            extract_redacted=_real_extract_redacted_bytes,
            extract_redacted_from_file=lambda core: redact_dmesg(
                core, read_core_dmesg_from_file, secret_registry
            ),
            host_dump_capture=_real_host_dump_capture,
            fetch_object=default_fetch_object,
            run_crash=default_run_crash,
            secret_registry=secret_registry,
        )

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Capture a core via ``method``; store raw + redacted; return refs + build-id.

        ``KDUMP`` streams the harvested core from a worker temp file straight to the object
        store, never holding the whole core in one in-memory buffer (#657); ``HOST_DUMP``
        keeps the bytes path.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for capture/build-id provenance or
                input failures propagated by injected seams; ``MISSING_DEPENDENCY`` when a
                capture, build-id, or redaction seam is unavailable; ``READINESS_FAILURE``
                if no complete core appears in the window; or ``INFRASTRUCTURE_FAILURE``
                propagated from a failed artifact store.
        """
        if method is CaptureMethod.HOST_DUMP:
            return self._capture_host_dump(system_id, method)
        return self._capture_kdump(system_id, method)

    def _capture_kdump(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        core = self._wait_for_vmcore(system_id)
        if core is None:
            raise self._no_core(system_id)
        try:
            build_id = self._read_vmcore_build_id_from_file(core)
            raw = self._put_stream(system_id, f"vmcore-{method.value}", core)
            redacted = self._put(
                system_id,
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
        finally:
            _remove_spool(core)

    def _capture_host_dump(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        data = self._host_dump_capture(system_id)
        if data is None:
            raise self._no_core(system_id)
        build_id = self._read_vmcore_build_id(data)
        raw = self._put(system_id, f"vmcore-{method.value}", data, Sensitivity.SENSITIVE)
        redacted = self._put(
            system_id,
            f"vmcore-{method.value}-redacted",
            self._extract_redacted(data),
            Sensitivity.REDACTED,
        )
        return CaptureOutput(
            raw=raw, redacted=redacted, vmcore_build_id=build_id, raw_size_bytes=len(data)
        )

    @staticmethod
    def _no_core(system_id: UUID) -> CategorizedError:
        return CategorizedError(
            "no complete core appeared within the capture window",
            category=ErrorCategory.READINESS_FAILURE,
            details={"system_id": str(system_id)},
        )

    def _put_stream(self, system_id: UUID, name: str, core: Path) -> StoredArtifact:
        """Stream ``core`` to the object store and verify the stored checksum (ADR-0094)."""
        sha256_b64 = file_sha256_b64(core)
        store = self._ensure_store()
        stored = store.put_stream(
            ArtifactStreamRequest(
                tenant=self._tenant,
                owner_kind="systems",
                owner_id=str(system_id),
                name=name,
                path=core,
                sha256_b64=sha256_b64,
                sensitivity=Sensitivity.SENSITIVE,
                retention_class=_RETENTION_CLASS,
            )
        )
        self._verify_stored(stored.key, sha256_b64, system_id)
        return stored

    def _verify_stored(self, key: str, sha256_b64: str, system_id: UUID) -> None:
        head = self._ensure_store().head(key)
        if head is None:
            raise CategorizedError(
                "stored kdump core is absent after a success-reporting put",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "key": key},
            )
        if head.checksum_sha256 is not None and head.checksum_sha256 != sha256_b64:
            raise CategorizedError(
                "stored kdump core checksum does not match the streamed core",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "key": key},
            )

    def _ensure_store(self) -> _StorePort:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    def _put(self, system_id: UUID, name: str, data: bytes, sens: Sensitivity) -> StoredArtifact:
        return self._ensure_store().put_artifact(
            ArtifactWriteRequest(
                tenant=self._tenant,
                owner_kind="systems",
                owner_id=str(system_id),
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
            expected_build_id=expected_build_id,
            commands=commands,
            fetch_object=self._fetch_object,
            read_build_id=self._read_vmcore_build_id,
            run_crash=self._run_crash,
            secret_registry=self._secret_registry,
        )


_VAR_CRASH_GLOB = "/var/crash/*/vmcore"


def _libvirt_uri() -> str:
    """The provider's configured libvirt URI (``KDIVE_LIBVIRT_URI``, default ``qemu:///system``)."""
    return config.require(LIBVIRT_URI)


class _LibguestfsCoreReader:  # pragma: no cover - live_vm (libguestfs)
    """Read-only libguestfs view of a System's overlay, listing/reading /var/crash cores.

    The libguestfs appliance is launched once in the constructor and reused for both the
    listing and the read; the caller closes it via ``close()``. Every guestfs call is wrapped
    so a corrupt/locked overlay or a vanished core surfaces as a typed ``CategorizedError``
    (the provider contract), not a raw ``guestfs.Error``.
    """

    def __init__(self, overlay: str) -> None:
        self._overlay = overlay
        self._guest = self._mount(overlay)

    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]:
        try:
            entries: list[VmcoreEntry] = []
            for path in self._guest.glob_expand(_VAR_CRASH_GLOB):
                stat = self._guest.statns(path)
                entries.append(
                    VmcoreEntry(path=path, mtime=stat["st_mtime_sec"], size_bytes=stat["st_size"])
                )
            return entries
        except Exception as exc:
            raise self._io_failure("listing /var/crash cores", exc) from exc

    def download_vmcore(self, overlay: str, path: str, dest: Path) -> None:
        """Stream the core at ``path`` to ``dest`` (constant memory), not into RAM (#657)."""
        try:
            self._guest.download(path, str(dest))
        except Exception as exc:
            raise self._io_failure("downloading the kdump core", exc) from exc

    def close(self) -> None:
        try:
            self._guest.close()
        except Exception:
            _log.warning("libguestfs handle close failed; continuing", exc_info=True)

    def _io_failure(self, op: str, exc: Exception) -> CategorizedError:
        return CategorizedError(
            f"libguestfs failed {op} from the System overlay",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"overlay": self._overlay, "error": type(exc).__name__},
        )

    @staticmethod
    def _mount(overlay: str):  # type: ignore[no-untyped-def]
        try:
            import guestfs  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
        except ImportError as exc:
            raise CategorizedError(
                "libguestfs (the guestfs Python binding) is required for local kdump capture",
                category=ErrorCategory.MISSING_DEPENDENCY,
            ) from exc
        guest = guestfs.GuestFS(python_return_dict=True)
        try:
            guest.add_drive_opts(overlay, readonly=1)
            guest.launch()
            roots = guest.inspect_os()
        except Exception as exc:
            guest.close()
            raise CategorizedError(
                "libguestfs failed to open the System overlay",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"overlay": overlay, "error": type(exc).__name__},
            ) from exc
        if not roots:
            guest.close()
            raise CategorizedError(
                "could not inspect the System overlay to find /var/crash",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"overlay": overlay},
            )
        guest.mount_ro(roots[0], "/")
        return guest


def _force_off_domain(system_id: UUID) -> None:  # pragma: no cover - live_vm (libvirt)
    """Force the System's domain off (idempotent) so its overlay is safe to read offline.

    Opens the provider's configured URI (``KDIVE_LIBVIRT_URI``), the same source as
    ``control.py``/``discovery.py`` — never ``libvirt.open(None)``. ``vmcore.fetch`` admits
    only on a ``CRASHED`` System, so a force-off is consistent with its state, and libguestfs
    reads of a disk a running guest is mutating are unsafe (ADR-0203).
    """
    conn = libvirt.open(_libvirt_uri())
    try:
        try:
            domain = conn.lookupByName(domain_name_for(system_id))
        except libvirt.libvirtError:
            return  # already gone — nothing running to quiesce
        if domain.isActive():
            domain.destroy()
    finally:
        conn.close()


def _real_wait_for_vmcore(system_id: UUID) -> Path | None:  # pragma: no cover - live_vm
    """Harvest the newest overlay core to a worker temp file; ``None`` when none exists.

    Owns the spool's lifecycle only up to a successful return: it creates a private temp
    directory, streams the chosen core into it, and removes the directory if no core is found
    or the harvest raises. On a successfully returned path the caller (``capture``) owns
    cleanup, so the file survives this function's ``finally`` (it is on the host filesystem,
    independent of the libguestfs appliance handle).
    """
    _force_off_domain(system_id)
    overlay = overlay_path(system_id)
    spool_dir = Path(tempfile.mkdtemp(prefix="kdive-kdump-"))
    dest = spool_dir / "vmcore"
    reader = _LibguestfsCoreReader(overlay)
    core: Path | None = None
    try:
        core = harvest_vmcore(reader, overlay, dest=dest, max_bytes=MAX_CORE_BYTES)
        return core
    finally:
        reader.close()
        if core is None:
            _remove_spool(dest)


def _remove_spool(core: Path) -> None:
    """Remove the spooled core and the private temp directory holding it (best effort)."""
    shutil.rmtree(core.parent, ignore_errors=True)


def _real_read_build_id(data: bytes) -> str:  # pragma: no cover - live_vm (drgn)
    return read_via_tempfile(data, read_core_build_id_from_file)


def _real_extract_redacted_bytes(data: bytes) -> bytes:  # pragma: no cover - live_vm
    raise CategorizedError(
        "local host_dump redaction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


def _real_host_dump_capture(system_id: UUID) -> bytes | None:  # pragma: no cover - live_vm
    raise CategorizedError(
        "real host-dump capture runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system_id": str(system_id)},
    )


__all__ = [
    "LocalLibvirtRetrieve",
]
