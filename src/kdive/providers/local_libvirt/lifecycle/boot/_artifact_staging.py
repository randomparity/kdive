"""Exact-version local artifact staging and immutable publication (ADR-0586)."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import cast

from kdive.build_artifacts import validation as build_validation
from kdive.domain.external_boot_values import (
    ActivationOwnership,
    ArtifactSource,
    DebuginfoSource,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    InitrdSource,
    KernelIdentity,
    MaterializedArtifacts,
)
from kdive.providers.local_libvirt.lifecycle.boot import recovery as recovery_validation
from kdive.providers.local_libvirt.lifecycle.boot.kernel_bundle import (
    convert_kernel_bundle_modules,
    extract_kernel_bundle,
)
from kdive.providers.local_libvirt.lifecycle.boot.recovery_store import (
    _OWNED_TEMPORARY_NAMES,
    _PROJECTION_NAME,
    LocalPreStopIntentV1,
    TargetProjectionStore,
    TargetProjectionV1,
    _projection_ref,
    _read_private_file,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    LocalExternalBootSession,
    PersistentDomainInspection,
)
from kdive.providers.ports.external_boot import ExternalBootArtifactStager
from kdive.providers.shared.directory_listing import list_directory_descriptor
from kdive.providers.shared.external_boot_bounds import source_byte_limit as _source_byte_limit
from kdive.providers.shared.libvirt_external_boot import render_target_xml
from kdive.store.objectstore import ObjectStore


class _ExactVersionDescriptorStore:
    """Ranged validation view over one immutable object version and one local descriptor."""

    def __init__(self, key: str, version: str, descriptor: int, size: int) -> None:
        self._key = key
        self._version = version
        self._descriptor = descriptor
        self._size = size

    def get_range(
        self, key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes:
        if key != self._key or version_id not in {None, self._version}:
            raise ValueError("artifact validation requested a different object version")
        return os.pread(self._descriptor, min(length, self._size - start), start)


# local-libvirt's platform root device (ProviderRuntime.platform_root_cmdline, ADR-0183).
_LOCAL_WHOLE_DISK_ROOT = "root=/dev/vda"


def _whole_disk_root_uuid(plan: ExternalBootPlan) -> str | None:
    """Return the filesystem UUID a plan naming the whole-disk root must prove, else ``None``.

    A plan without an initrd names the local whole-disk device instead of the inspected
    ``UUID=`` token (ADR-0583 amendment); prepare proves that filesystem fills the disk.
    """
    if f"root={plan.root.root}" in plan.platform_arguments:
        return None
    if _LOCAL_WHOLE_DISK_ROOT not in plan.platform_arguments:
        raise ValueError(f"external-boot direct root must be local {_LOCAL_WHOLE_DISK_ROOT}")
    if not plan.root.root.startswith("UUID="):
        raise ValueError("external-boot direct root requires an inspected filesystem UUID")
    return plan.root.root.removeprefix("UUID=")


class RealLocalExternalBootMaterializer(ExternalBootArtifactStager):
    """Materialize exact object versions into one authenticated activation projection."""

    def __init__(self, object_store: ObjectStore) -> None:
        self._object_store = object_store

    def materialize(
        self, plan: ExternalBootPlan, session: LocalExternalBootSession
    ) -> ExternalBootMaterialization:
        binding = session.binding
        if binding.system_id != plan.ownership.system_id or binding.run_id != plan.ownership.run_id:
            raise ValueError("external-boot plan does not match session ownership")
        projection = TargetProjectionV1(
            ownership={"system_id": binding.system_id, "run_id": binding.run_id},
            activation_id=binding.activation_id,
            plan_identity=plan.identity,
            architecture=plan.architecture,
            cmdline=plan.cmdline,
            initrd_filename=None if plan.initrd is None else "initrd",
            whole_disk_root_uuid=_whole_disk_root_uuid(plan),
            debuginfo_filename=None if plan.debuginfo is None else "debuginfo",
        )
        with session.projection_directory(projection) as directory_fd:
            try:
                reopened = TargetProjectionStore.reopen_at(directory_fd, projection)
            except FileNotFoundError:
                try:
                    evidence, installed_manifest = self.materialize_artifacts(plan, directory_fd)
                    self._fetch_debuginfo(plan, directory_fd)
                    self._validate_local_debuginfo(plan, directory_fd)
                except BaseException as primary:
                    _cleanup_uncommitted_payloads(directory_fd, primary)
                    raise
                TargetProjectionStore.publish_at(directory_fd, projection)
                reopened = TargetProjectionStore.reopen_at(directory_fd, projection)
            else:
                evidence, installed_manifest = self.validate_materialized_artifacts(
                    plan, directory_fd
                )
                self._validate_local_debuginfo(plan, directory_fd)
            if reopened != projection:
                raise ValueError("materialized target projection changed on exact reopen")
        return ExternalBootMaterialization(
            architecture=plan.architecture,
            provider_kind="local-libvirt",
            ownership=projection.ownership,
            plan_identity=plan.identity,
            extracted_vmlinuz_sha256=str(evidence["vmlinuz_sha256"]),
            source_module_manifest=str(evidence["module_source_manifest"]),
            installed_module_tree=installed_manifest,
            verified_bundle_sha256=plan.bundle.sha256,
            verified_initrd_sha256=None if plan.initrd is None else plan.initrd.sha256,
            verified_debuginfo_sha256=None if plan.debuginfo is None else plan.debuginfo.sha256,
            kernel_observation=KernelIdentity(
                architecture=plan.architecture,
                release=str(evidence["release"]),
                gnu_build_id=str(evidence["gnu_build_id"]),
            ),
            artifacts=MaterializedArtifacts(
                kernel=_projection_ref(projection, "kernel"),
                modules=_projection_ref(projection, "modules"),
                initrd=(None if plan.initrd is None else _projection_ref(projection, "initrd")),
                debuginfo=(
                    None if plan.debuginfo is None else _projection_ref(projection, "debuginfo")
                ),
            ),
        )

    def materialize_artifacts(
        self, plan: ExternalBootPlan, directory_fd: int
    ) -> tuple[dict[str, object], str]:
        """Write and independently revalidate exact artifacts in an owner-selected directory."""
        self._fetch_and_validate(plan, directory_fd)
        return self.validate_materialized_artifacts(plan, directory_fd)

    def validate_materialized_artifacts(
        self, plan: ExternalBootPlan, directory_fd: int
    ) -> tuple[dict[str, object], str]:
        """Reopen and validate previously materialized exact artifacts without writing them."""
        evidence, installed_manifest = self._validate_local_bundle(plan, directory_fd)
        self._validate_local_initrd(plan, directory_fd)
        return evidence, installed_manifest

    def inspect_prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        inspection: PersistentDomainInspection,
        session: LocalExternalBootSession,
    ) -> LocalPreStopIntentV1:
        if binding != session.binding or materialization.ownership != ActivationOwnership(
            system_id=binding.system_id, run_id=binding.run_id
        ):
            raise ValueError("external-boot preparation does not match session ownership")
        projection = session.reopen_projection(materialization.artifacts.kernel)
        if (
            projection.plan_identity != materialization.plan_identity
            or projection.architecture != materialization.architecture
            or projection.activation_id != binding.activation_id
            or materialization.artifacts.modules != _projection_ref(projection, "modules")
            or materialization.artifacts.initrd
            != (
                None
                if projection.initrd_filename is None
                else _projection_ref(projection, "initrd")
            )
            or materialization.artifacts.debuginfo
            != (
                None
                if projection.debuginfo_filename is None
                else _projection_ref(projection, "debuginfo")
            )
        ):
            raise ValueError("external-boot materialization does not match target projection")
        with session.projection_directory(projection) as directory_fd:
            modules_digest, modules_bytes = _descriptor_digest(directory_fd, "modules")
            installed_manifest = _installed_module_manifest(directory_fd)
            kernel_digest, _ = _descriptor_digest(directory_fd, "kernel")
            if kernel_digest != materialization.extracted_vmlinuz_sha256:
                raise ValueError("materialized kernel bytes do not match materialization")
            if installed_manifest != materialization.installed_module_tree:
                raise ValueError("materialized module tree does not match materialization")
        source_xml = inspection.xml.decode()
        kernel_path = session.projection_artifact_path(projection, "kernel")
        initrd_path = (
            None
            if projection.initrd_filename is None
            else session.projection_artifact_path(projection, "initrd")
        )
        target_xml = render_target_xml(
            source_xml, kernel=kernel_path, initrd=initrd_path, cmdline=projection.cmdline
        )
        target_xml_sha256 = "sha256:" + hashlib.sha256(target_xml.encode()).hexdigest()
        return LocalPreStopIntentV1(
            binding=binding,
            plan_identity=materialization.plan_identity,
            materialization_identity=materialization.identity,
            release=materialization.kernel_observation.release,
            materialized_modules=materialization.artifacts.modules,
            materialized_modules_sha256=modules_digest,
            materialized_modules_bytes=modules_bytes,
            source_xml_sha256="sha256:" + hashlib.sha256(inspection.xml).hexdigest(),
            source_xml=source_xml,
            source_definition=inspection.definition_identity,
            source_boot=inspection.source_boot_identity,
            target_boot=session.boot_identity(target_xml),
            target_projection_sha256=projection.digest,
            target_xml_sha256=target_xml_sha256,
            target_xml=target_xml,
            expected_running=materialization.kernel_observation,
            prior_power="running" if inspection.active else "inactive",
        )

    def _fetch_and_validate(self, plan: ExternalBootPlan, directory_fd: int) -> None:
        temporary_directory = f"/proc/self/fd/{directory_fd}"
        bundle_fd = _stream_exact_version(
            self._object_store, plan.bundle, directory_fd, ".bundle.next"
        )
        try:
            self._validate_bundle_evidence(plan, bundle_fd)
            os.lseek(bundle_fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(bundle_fd), "rb") as source:
                base = Path(f"/proc/self/fd/{directory_fd}")
                extract_kernel_bundle(source, base / ".kernel.next", None)
            _make_private_temporary(directory_fd, ".kernel.next")
            _remove_private_temporary(directory_fd, ".modules.next")
            modules_fd = os.open(
                ".modules.next",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.lseek(bundle_fd, 0, os.SEEK_SET)
                with (
                    os.fdopen(os.dup(bundle_fd), "rb") as source,
                    os.fdopen(os.dup(modules_fd), "wb") as destination,
                ):
                    convert_kernel_bundle_modules(
                        source,
                        destination,
                        release=plan.module_obligation.release,
                        temporary_directory=temporary_directory,
                    )
                os.fsync(modules_fd)
            finally:
                os.close(modules_fd)
            _commit_private_artifact(directory_fd, ".kernel.next", "kernel")
            _commit_private_artifact(directory_fd, ".modules.next", "modules")
        finally:
            os.close(bundle_fd)
            with suppress(FileNotFoundError):
                os.unlink(".bundle.next", dir_fd=directory_fd)
        if plan.initrd is not None:
            initrd_fd = _stream_exact_version(
                self._object_store, plan.initrd, directory_fd, ".initrd.next"
            )
            os.close(initrd_fd)
            _commit_private_artifact(directory_fd, ".initrd.next", "initrd")

    def _fetch_debuginfo(self, plan: ExternalBootPlan, directory_fd: int) -> None:
        # Local projections only: the remote authorities share `materialize_artifacts` through
        # `ExternalBootArtifactStager` and do not stage the vmlinux (#3131).
        if plan.debuginfo is None:
            return
        debuginfo_fd = _stream_exact_version(
            self._object_store, plan.debuginfo, directory_fd, ".debuginfo.next"
        )
        os.close(debuginfo_fd)
        _commit_private_artifact(directory_fd, ".debuginfo.next", "debuginfo")

    @staticmethod
    def _validate_bundle_evidence(plan: ExternalBootPlan, descriptor: int) -> dict[str, object]:
        size = os.fstat(descriptor).st_size
        store = _ExactVersionDescriptorStore(plan.bundle.key, plan.bundle.version, descriptor, size)
        evidence = build_validation._scan_external_boot_archive(  # noqa: SLF001
            cast(build_validation.ValidatorStore, store),
            plan.bundle.key,
            size,
            plan.architecture,
        )
        expected = {
            "archive_member_count": plan.bundle.member_count,
            "archive_uncompressed_bytes": plan.bundle.uncompressed_bytes,
            "vmlinuz_sha256": plan.bundle.vmlinuz_sha256,
            "vmlinuz_size_bytes": plan.bundle.vmlinuz_size_bytes,
            "decoded_kernel_size_bytes": plan.bundle.decoded_kernel_size_bytes,
            "elf_metadata_bytes": plan.bundle.elf_metadata_bytes,
            "gnu_build_id_size_bytes": plan.bundle.gnu_build_id_size_bytes,
            "module_source_manifest": plan.module_obligation.source_manifest,
            "module_member_count": plan.module_obligation.member_count,
            "module_uncompressed_bytes": plan.module_obligation.uncompressed_bytes,
            "release": plan.module_obligation.release,
            "architecture": plan.architecture,
        }
        if any(evidence[key] != value for key, value in expected.items()):
            raise ValueError("kernel bundle evidence does not match external-boot plan")
        return cast(dict[str, object], evidence)

    def _validate_local_bundle(
        self, plan: ExternalBootPlan, directory_fd: int
    ) -> tuple[dict[str, object], str]:
        temporary_directory = f"/proc/self/fd/{directory_fd}"
        bundle_fd = _stream_exact_version(
            self._object_store, plan.bundle, directory_fd, ".bundle.verify"
        )
        try:
            evidence = self._validate_bundle_evidence(plan, bundle_fd)
            os.lseek(bundle_fd, 0, os.SEEK_SET)
            with (
                open(os.devnull, "wb") as expected_modules,
                os.fdopen(os.dup(bundle_fd), "rb") as source,
            ):
                expected_modules_digest, expected_modules_size = convert_kernel_bundle_modules(
                    source,
                    expected_modules,
                    release=plan.module_obligation.release,
                    temporary_directory=temporary_directory,
                )
        finally:
            os.close(bundle_fd)
            with suppress(FileNotFoundError):
                os.unlink(".bundle.verify", dir_fd=directory_fd)
        kernel_digest, kernel_size = _descriptor_digest(directory_fd, "kernel")
        if (
            kernel_digest != plan.bundle.vmlinuz_sha256
            or kernel_size != plan.bundle.vmlinuz_size_bytes
        ):
            raise ValueError("materialized kernel bytes do not match external-boot plan")
        modules_digest, modules_size = _descriptor_digest(directory_fd, "modules")
        if (modules_digest, modules_size) != (expected_modules_digest, expected_modules_size):
            raise ValueError("materialized canonical module archive does not match exact bundle")
        installed_manifest = _installed_module_manifest(directory_fd)
        return evidence, installed_manifest

    def _validate_local_initrd(self, plan: ExternalBootPlan, directory_fd: int) -> None:
        if plan.initrd is None:
            return
        verify_fd = _stream_exact_version(
            self._object_store, plan.initrd, directory_fd, ".initrd.verify"
        )
        os.close(verify_fd)
        with suppress(FileNotFoundError):
            os.unlink(".initrd.verify", dir_fd=directory_fd)
        digest, size = _descriptor_digest(directory_fd, "initrd")
        if digest != plan.initrd.sha256 or size != plan.initrd.size_bytes:
            raise ValueError("materialized initrd bytes do not match external-boot plan")

    @staticmethod
    def _validate_local_debuginfo(plan: ExternalBootPlan, directory_fd: int) -> None:
        # Unlike the initrd, the object is not streamed a second time: a second copy of up to
        # 1.5 GiB is outside the materialization reservation (#3130).
        if plan.debuginfo is None:
            return
        digest, size = _descriptor_digest(directory_fd, "debuginfo")
        if digest != plan.debuginfo.sha256 or size != plan.debuginfo.size_bytes:
            raise ValueError("materialized debuginfo bytes do not match external-boot plan")


def _stream_exact_version(
    store: ObjectStore, source: ArtifactSource, directory_fd: int, name: str
) -> int:
    """Stream and digest one exact object version into an exclusive private child."""
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except FileExistsError:
        descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory_fd)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
            os.close(descriptor)
            raise ValueError("interrupted artifact is not a private regular file") from None
        digest, _ = _open_descriptor_digest(descriptor)
        if digest != source.sha256:
            os.close(descriptor)
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        else:
            return descriptor
    primary: BaseException | None = None
    digest = hashlib.sha256()
    limit = _source_byte_limit(source)
    size = 0
    try:
        with store.get_artifact_stream(source.key, None, version_id=source.version) as streamed:
            while chunk := streamed.reader.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ValueError("exact object version exceeds its materialization byte bound")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("artifact write made no progress")
                    view = view[written:]
        if "sha256:" + digest.hexdigest() != source.sha256:
            raise ValueError("exact object version digest does not match external-boot plan")
        if isinstance(source, InitrdSource | DebuginfoSource) and size != source.size_bytes:
            raise ValueError("exact object version size does not match external-boot plan")
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if primary is not None:
            try:
                os.close(descriptor)
            except OSError as close_error:
                primary.add_note(f"artifact descriptor cleanup failed: {close_error!r}")
            try:
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileNotFoundError:
                pass
            except OSError as unlink_error:
                primary.add_note(f"partial artifact cleanup failed: {unlink_error!r}")


def _commit_private_artifact(directory_fd: int, temporary: str, final: str) -> None:
    """Link an immutable artifact into place without replacing an existing result."""
    _make_private_temporary(directory_fd, temporary)
    _commit_private_artifact_link(directory_fd, temporary, final)


def _make_private_temporary(directory_fd: int, temporary: str) -> None:
    temporary_fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        os.fchmod(temporary_fd, 0o600)
        os.fsync(temporary_fd)
    finally:
        os.close(temporary_fd)


def _remove_private_temporary(directory_fd: int, temporary: str) -> None:
    try:
        descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
            raise ValueError("interrupted derived artifact is not a private regular file")
    finally:
        os.close(descriptor)
    os.unlink(temporary, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _commit_private_artifact_link(directory_fd: int, temporary: str, final: str) -> None:
    try:
        os.link(
            temporary,
            final,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileExistsError:
        temporary_digest, temporary_size = _descriptor_digest(directory_fd, temporary)
        final_digest, final_size = _descriptor_digest(directory_fd, final)
        if (temporary_digest, temporary_size) != (final_digest, final_size):
            raise ValueError(
                "immutable materialized artifact conflicts with existing bytes"
            ) from None
    os.unlink(temporary, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _open_descriptor_digest(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return "sha256:" + digest.hexdigest(), size


def _descriptor_digest(directory_fd: int, name: str) -> tuple[str, int]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
            raise ValueError("materialized artifact is not a private regular file")
        return _open_descriptor_digest(descriptor)
    finally:
        os.close(descriptor)


def _installed_module_manifest(directory_fd: int) -> str:
    modules_fd = os.open("modules", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        with os.fdopen(os.dup(modules_fd), "rb") as modules:
            entries = recovery_validation._validate_archive(modules)  # noqa: SLF001
        return recovery_validation._manifest(entries)[1]  # noqa: SLF001
    finally:
        os.close(modules_fd)


def _cleanup_uncommitted_payloads(directory_fd: int, primary: BaseException) -> None:
    """Remove only exact private payload names while no projection commit exists."""
    try:
        _read_private_file(directory_fd, _PROJECTION_NAME)
    except FileNotFoundError:
        pass
    except BaseException as cleanup:
        primary.add_note(f"uncommitted payload cleanup refused: {cleanup!r}")
        return
    else:
        primary.add_note("uncommitted payload cleanup refused: projection is committed")
        return
    entries = set(list_directory_descriptor(directory_fd))
    allowed = {"kernel", "modules", "initrd", "debuginfo", *_OWNED_TEMPORARY_NAMES}
    if not entries <= allowed:
        primary.add_note("uncommitted payload cleanup refused: projection has unknown entries")
        return
    for name in sorted(entries):
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
                    raise ValueError(f"uncommitted payload {name!r} is not a private regular file")
            finally:
                os.close(descriptor)
        except BaseException as cleanup:
            primary.add_note(f"uncommitted payload cleanup refused: {cleanup!r}")
            return
    for name in sorted(entries):
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            continue
        except OSError as cleanup:
            primary.add_note(f"uncommitted payload cleanup failed: {cleanup!r}")
            return
    try:
        os.fsync(directory_fd)
    except OSError as cleanup:
        primary.add_note(f"uncommitted payload cleanup fsync failed: {cleanup!r}")
