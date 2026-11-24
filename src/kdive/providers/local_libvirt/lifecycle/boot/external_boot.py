"""Local-libvirt external-boot recovery state machine (ADRs 0586, 0601)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from kdive.domain.external_boot_timing import LocalExternalBootTimingV1
from kdive.domain.external_boot_values import (
    AbsentComponentState,
    ComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPreparationObservation,
    ExternalBootPreparationRequest,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryObjectBinding,
    RecoveryObjectObservation,
    RecoveryPoint,
    RunningKernelObservation,
)
from kdive.providers.external_boot_authority.stranded import StrandedAuthorityFacts
from kdive.providers.external_boot_authority.teardown import (
    AuthoritySystemTeardownFacts,
    AuthorityTeardownReservationV1,
    ProviderRecoveryRefusal,
    SystemTeardownSupersededError,
)
from kdive.providers.local_libvirt.lifecycle.boot import recovery as recovery_validation
from kdive.providers.local_libvirt.lifecycle.boot._artifact_staging import _open_descriptor_digest
from kdive.providers.local_libvirt.lifecycle.boot.guest_free_space import (
    require_free_space_for_vmlinux,
)
from kdive.providers.local_libvirt.lifecycle.boot.readiness import ReadinessResult
from kdive.providers.local_libvirt.lifecycle.boot.recovery import (
    MAX_ENTRIES,
    AbsentModuleCapture,
    GuestRecoveryWriter,
    GuestTreeEntry,
    KernelBundleSource,
    ModuleArchiveCapture,
    ModuleCapture,
    RecoveryArchiveSource,
)
from kdive.providers.local_libvirt.lifecycle.boot.recovery_store import (
    _MAX_PROJECTION_BYTES,
    CleanupQuarantineReceiptV1,
    CleanupTombstoneV1,
    Digest,
    FinalizeCleanupProof,
    LocalPreStopIntentV1,
    LocalRecoveryMetadataV1,
    LocalSystemTeardownIntentV1,
    RecoveryMetadataStore,
    RecoveryPhase,
    SystemTeardownPhase,
    TargetProjectionV1,
    _canonical_record_bytes,
    _LocalSystemTeardownFields,
    _recovery_ref,
    recovery_point_digest,
)
from kdive.providers.local_libvirt.lifecycle.boot.selinux_policy import (
    ModuleLabelPolicy,
    guest_policy,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    ClosedDomainInspection,
    ExpectedOperationOwnership,
    InactiveGuest,
    LocalExternalBootOperationLease,
    LocalExternalBootSession,
    LocalExternalBootSessionFactory,
    LocalSystemTeardownInspection,
    LocalSystemTeardownSession,
    StopMode,
    TreeCursor,
)
from kdive.providers.shared.external_boot_bounds import materialization_reservation_bytes
from kdive.providers.shared.libvirt_external_boot import (
    boot_projection_element_identity,
    parse_projected_domain_xml,
    preserved_element_identity,
)
from kdive.providers.shared.runtime_paths import domain_name_for


class PublicationPhase(StrEnum):
    MOVE_READY = "move-ready"
    OLD_ASIDE = "old-aside"
    ROLLBACK_READY = "rollback-ready"
    ROLLBACK_COMPLETE = "rollback-complete"
    NEW_LIVE = "new-live"
    PUBLICATION_COMPLETE = "publication-complete"
    ABSENCE_LIVE = "absence-live"
    ABSENCE_COMPLETE = "absence-complete"
    ABSENCE_CLEANED = "absence-cleaned"


type PartialAbortResult = Literal["removed", "absent", "not-partial"]
_MAX_RECOVERY_METADATA_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class ModuleLayout:
    live: ComponentState | None
    staging: ComponentState | None
    old: ComponentState | None


class ModulePublicationIO(Protocol):
    def require_inactive(self) -> None: ...
    def observe_layout(self) -> ModuleLayout: ...
    def move_live_to_old(self) -> None: ...
    def move_staging_to_live(self) -> None: ...
    def move_old_to_live(self) -> None: ...
    def remove_old(self) -> None: ...
    def guest_sync(self) -> None: ...
    def record_phase(self, phase: PublicationPhase) -> None: ...


class _GuestfsTreeHandle(Protocol):  # pragma: no cover - live_vm (libguestfs binding)
    def exists(self, path: str) -> int: ...
    def is_dir(self, path: str, *, followsymlinks: bool) -> int: ...
    def open_tree(self, path: str, *, limit: int) -> TreeCursor: ...
    def lstatns(self, path: str) -> dict[str, int]: ...
    def readlink(self, path: str) -> str: ...
    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]: ...
    def open_regular(self, path: str, *, size: int) -> AbstractContextManager[BinaryIO]: ...
    def create_regular(self, content: BinaryIO, path: str, *, size: int) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def ln_s(self, target: str, linkname: str) -> None: ...
    def chmod(self, mode: int, path: str) -> None: ...
    def chown(self, owner: int, group: int, path: str) -> None: ...
    def lsetxattr(self, xattr: str, val: str, vallen: int, path: str) -> None: ...
    def mv(self, source: str, destination: str) -> None: ...
    def rm_rf(self, path: str) -> None: ...
    def sync(self) -> None: ...


class LibguestfsAuthenticatedGuestTree:
    """Private, owner-bound capability for one deterministic guest tree."""

    def __init__(
        self,
        guest: _GuestfsTreeHandle,
        *,
        binding: ExternalBootActivationBinding,
        release: str,
        root: str,
        mutable: bool,
        label_policy: ModuleLabelPolicy | None = None,
    ) -> None:
        expected_prefix = f"/lib/modules/.kdive-{binding.activation_id}-"
        live = f"/lib/modules/{release}"
        if root != live and not root.startswith(expected_prefix):
            raise ValueError("guest-tree root is not the bound release or activation staging tree")
        if mutable and root == live:
            raise ValueError("mutable guest-tree capability requires an activation staging tree")
        if not release or "/" in release or unicodedata.normalize("NFC", release) != release:
            raise ValueError("guest-tree release is invalid")
        self._guest = guest
        self.binding = binding
        self.release = release
        self.mutable = mutable
        self._root = root
        self._label_policy = label_policy

    def root_kind(self) -> Literal["absent", "directory", "other"]:
        if not bool(self._guest.exists(self._root)):
            return "absent"
        if bool(self._guest.is_dir(self._root, followsymlinks=False)):
            return "directory"
        return "other"

    def entries(self) -> Iterator[GuestTreeEntry]:
        if self.root_kind() != "directory":
            return
        seen: set[str] = set()
        with self._guest.open_tree(self._root, limit=MAX_ENTRIES) as cursor:
            for index, entry in enumerate(cursor, start=1):
                if index > MAX_ENTRIES:
                    raise ValueError("guest tree exceeds the entry-count bound")
                path = _guest_relative(entry.path)
                if path in seen:
                    raise ValueError("guest tree contains duplicate paths")
                seen.add(path)
                yield self._entry(path)

    @contextmanager
    def open_regular(self, path: str, size: int) -> Generator[BinaryIO]:
        remote = self._remote(path)
        opened = self._guest.lstatns(remote)
        if opened["st_size"] != size or not stat.S_ISREG(opened["st_mode"]):
            raise ValueError("guest regular file changed before content read")
        with self._guest.open_regular(remote, size=size) as content:
            yield content

    def prepare_restore(self, entries: Iterator[GuestTreeEntry]) -> None:
        self._require_mutable()
        for entry in entries:
            for name, value in entry.xattrs.items():
                _xattr_text(name, value)
        self._guest.mkdir(self._root)

    def create_directory(self, entry: GuestTreeEntry) -> None:
        self._require_mutable()
        remote = self._remote(entry.path)
        self._guest.mkdir(remote)
        self._apply_metadata(remote, entry)

    def create_regular(self, entry: GuestTreeEntry, content: BinaryIO) -> None:
        self._require_mutable()
        self._guest.create_regular(content, self._remote(entry.path), size=entry.size)
        self._apply_metadata(self._remote(entry.path), entry)

    def create_symlink(self, entry: GuestTreeEntry) -> None:
        self._require_mutable()
        if entry.target is None:
            raise ValueError("guest symlink target is missing")
        remote = self._remote(entry.path)
        self._guest.ln_s(entry.target, remote)
        self._guest.chown(entry.uid, entry.gid, remote)
        self._apply_xattrs(remote, entry)
        self._apply_label(remote, entry)

    def remove_all(self) -> None:
        self._require_mutable()
        self._guest.rm_rf(self._root)

    def _entry(self, path: str) -> GuestTreeEntry:
        remote = self._remote(path)
        value = self._guest.lstatns(remote)
        mode = value["st_mode"]
        kind: Literal["directory", "regular", "symlink"]
        if stat.S_ISDIR(mode):
            kind = "directory"
        elif stat.S_ISREG(mode):
            kind = "regular"
        elif stat.S_ISLNK(mode):
            kind = "symlink"
        else:
            raise ValueError("guest tree contains an unsupported entry type")
        xattrs = self._guest.lgetxattrs(remote)
        return GuestTreeEntry(
            path=path,
            kind=kind,
            mode=f"{stat.S_IMODE(mode):04o}",
            uid=value["st_uid"],
            gid=value["st_gid"],
            size=value["st_size"] if kind == "regular" else 0,
            target=self._guest.readlink(remote) if kind == "symlink" else None,
            # With no xattrs, True and False describe the same content. Report False then, as
            # the canonical module archive does, so an installed tree reads back unchanged.
            xattrs_supported=bool(xattrs),
            xattrs={str(item["attrname"]): _xattr_bytes(item["attrval"]) for item in xattrs},
            link_count=value["st_nlink"],
        )

    def _apply_metadata(self, remote: str, entry: GuestTreeEntry) -> None:
        self._guest.chmod(int(entry.mode, 8), remote)
        self._guest.chown(entry.uid, entry.gid, remote)
        self._apply_xattrs(remote, entry)
        self._apply_label(remote, entry)

    def _apply_xattrs(self, remote: str, entry: GuestTreeEntry) -> None:
        for name, value in entry.xattrs.items():
            self._guest.lsetxattr(name, _xattr_text(name, value), len(value), remote)

    def _apply_label(self, remote: str, entry: GuestTreeEntry) -> None:
        if self._label_policy is None:
            return
        label = self._label_policy.label(
            f"/lib/modules/{self.release}/{entry.path}", _entry_mode(entry.kind, entry.mode)
        )
        self._guest.lsetxattr("security.selinux", label[:-1].decode("utf-8"), len(label), remote)

    def _remote(self, relative: str) -> str:
        return f"{self._root}/{_guest_relative(relative)}"

    def _require_mutable(self) -> None:
        if not self.mutable:
            raise ValueError("guest-tree capability is read-only")


def _guest_relative(path: str) -> str:
    if (
        not path
        or path.startswith("/")
        or unicodedata.normalize("NFC", path) != path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("guest-tree entry path is not a canonical relative path")
    return path


def _xattr_text(name: str, value: bytes) -> str:
    if name == "security.selinux":
        if not value[:-1] or not value.endswith(b"\0") or b"\0" in value[:-1]:
            raise ValueError("captured SELinux label is not a NUL-terminated context")
        try:
            return value[:-1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("captured SELinux label is not UTF-8") from exc
    remediation = (
        "retained capture requires operator-assisted recovery; "
        "retrying unchanged capture cannot fix it"
    )
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"unsupported captured non-UTF-8 xattr: {remediation}") from exc
    if "\0" in text:
        raise ValueError(f"unsupported captured NUL-containing xattr: {remediation}")
    return text


def _xattr_bytes(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode()


@dataclass(frozen=True, slots=True)
class LocalObservedState:
    """A read of observed provider state that classifies instead of refusing.

    ``observe_running`` requires the exact running target and raises otherwise, so it
    cannot name the source, mixed, unreadable, or conflicting states an authority
    observation has to distinguish (ADR-0584). This read reports whatever is there and
    leaves classification to the caller. ``None`` means that half could not be read; it
    carries no provider message, path, or output.
    """

    definition: str | None
    modules: ComponentState | None
    active: bool | None


class LocalExternalBootOperation(Protocol):
    """One authenticated operation's privileged and durable capabilities."""

    def materialize(self, plan: ExternalBootPlan) -> ExternalBootMaterialization: ...
    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
    ) -> LocalRecoveryMetadataV1: ...
    def recovery_ref(self, binding: ExternalBootActivationBinding) -> OpaqueProviderRef: ...
    def reopen(self, recovery: RecoveryPoint) -> LocalRecoveryMetadataV1: ...
    def reopen_binding(self, binding: ExternalBootActivationBinding) -> LocalRecoveryMetadataV1: ...
    def reopen_cleanup_tombstone(
        self, binding: ExternalBootActivationBinding
    ) -> CleanupTombstoneV1: ...
    def observe_state(self, metadata: LocalRecoveryMetadataV1) -> LocalObservedState: ...
    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation: ...
    def recover_modules(
        self, metadata: LocalRecoveryMetadataV1, acknowledged: LocalObservedState | None = None
    ) -> None: ...
    def define_source(
        self, metadata: LocalRecoveryMetadataV1, acknowledged: LocalObservedState | None = None
    ) -> None: ...
    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def record_phase(
        self, metadata: LocalRecoveryMetadataV1, phase: RecoveryPhase
    ) -> LocalRecoveryMetadataV1: ...
    def cleanup_complete(self, recovery: RecoveryPoint) -> bool: ...
    def cleanup(self, metadata: LocalRecoveryMetadataV1, point_digest: Digest) -> None: ...


class LocalPartialRecoveryOperation(Protocol):
    def abort_preparation(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: Digest,
        source_identity: str,
        target_identity: str,
        authority: OpaqueProviderRef,
    ) -> PartialAbortResult: ...
    def abort_system_teardown_preparation(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: Digest,
        authority: OpaqueProviderRef,
    ) -> PartialAbortResult: ...
    def recovery_is_absent(self, binding: ExternalBootActivationBinding) -> bool: ...


class LocalExternalBootIO(Protocol):
    """Opens one authenticated capability for each public coordinator call."""

    def open(
        self,
        authority: OpaqueProviderRef,
        expected: ExpectedOperationOwnership,
        *,
        local_timing: LocalExternalBootTimingV1 | None = None,
    ) -> AbstractContextManager[LocalExternalBootOperation]: ...
    def open_pinned(
        self, authority: OpaqueProviderRef, expected: ExpectedOperationOwnership
    ) -> AbstractContextManager[None]:
        """Hold the operation lease for a store-only call: no domain, overlay or connection."""
        ...

    def finalize_tombstone(self, recovery: RecoveryPoint, proof: FinalizeCleanupProof) -> None: ...
    def record_cleanup_quarantine(
        self, recovery: RecoveryPoint, proof: FinalizeCleanupProof
    ) -> None: ...
    def read_cleanup_quarantine(
        self, binding: ExternalBootActivationBinding
    ) -> CleanupQuarantineReceiptV1 | None: ...
    def adopt_cleanup_quarantine(self, receipt: CleanupQuarantineReceiptV1) -> None: ...
    def observe_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation: ...
    def publish_preparation(
        self, receipt: ExternalBootPreparationObservation
    ) -> ExternalBootPreparationObservation: ...
    def adopt_preparation(
        self,
        request: ExternalBootPreparationRequest,
        predecessor: ExternalBootPreparationRequest,
        predecessor_receipt_identity: str,
    ) -> ExternalBootPreparationObservation: ...
    def preparation_materialization(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootMaterialization: ...


class LocalSystemTeardownIO(Protocol):
    """Additional local-only System teardown surface, separate from six-port recovery I/O."""

    def begin_system_teardown(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> AuthoritySystemTeardownFacts: ...
    def teardown_system(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> AuthoritySystemTeardownFacts: ...
    def observe_system_teardown(
        self, anchor: _LocalSystemTeardownFields, authority: OpaqueProviderRef
    ) -> AuthoritySystemTeardownFacts: ...
    def system_teardown_recovery_point(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> RecoveryPoint | None: ...
    def system_teardown_recovery_is_absent(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> bool: ...
    def discard_system_teardown_recovery(
        self,
        intent: LocalSystemTeardownIntentV1,
        recovery: RecoveryPoint,
        proof: FinalizeCleanupProof,
        authority: OpaqueProviderRef,
    ) -> None: ...


class LocalStrandedObservationIO(Protocol):
    """Read-only local facts about a stranded activation, separate from recovery I/O."""

    def stranded_facts(
        self, binding: ExternalBootActivationBinding, domain_name: str
    ) -> StrandedAuthorityFacts: ...


class LocalExternalBootMaterializer(Protocol):
    """Builds immutable local artifacts without owning guest recovery I/O."""

    def materialize(
        self, plan: ExternalBootPlan, session: LocalExternalBootSession
    ) -> ExternalBootMaterialization: ...
    def inspect_prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        inspection: ClosedDomainInspection,
        session: LocalExternalBootSession,
    ) -> LocalPreStopIntentV1: ...


def _require_whole_disk_root(guest: InactiveGuest, projection: TargetProjectionV1) -> None:
    expected = projection.whole_disk_root_uuid
    if expected is not None and guest.whole_disk_root_uuid() != expected:
        raise ValueError(
            f"external-boot without an initrd needs root filesystem UUID={expected} to fill the "
            "System disk; supply an initrd with the build or use a whole-disk rootfs"
        )


def _entry_mode(kind: str, mode: str) -> int:
    file_type = {
        "directory": stat.S_IFDIR,
        "regular": stat.S_IFREG,
        "symlink": stat.S_IFLNK,
    }[kind]
    return file_type | int(mode, 8)


def _prepared_module_manifest(
    descriptor: int, release: str, baseline: str, policy: ModuleLabelPolicy | None
) -> str:
    with os.fdopen(os.dup(descriptor), "rb") as source:
        entries = recovery_validation._validate_archive(source)  # noqa: SLF001
    if recovery_validation._manifest(entries)[1] != baseline:  # noqa: SLF001
        raise ValueError("materialized module archive changed before preparation")
    if policy is None:
        return baseline
    labelled = []
    for entry in entries:
        label = policy.label(
            f"/lib/modules/{release}/{entry.path}", _entry_mode(entry.kind, entry.mode)
        )
        labelled.append(
            entry.model_copy(
                update={
                    "xattrs_supported": True,
                    "xattrs": {
                        **entry.xattrs,
                        "security.selinux": base64.b64encode(label).decode().rstrip("="),
                    },
                }
            )
        )
    return recovery_validation._manifest(labelled)[1]  # noqa: SLF001


type ResolveOperationLease = Callable[[OpaqueProviderRef], LocalExternalBootOperationLease]


class RealLocalExternalBootIO:
    """Concrete durable adapter that orders host mutations behind recovery evidence."""

    def __init__(
        self,
        recovery_root: Path,
        materializer: LocalExternalBootMaterializer,
        recovery_writer: GuestRecoveryWriter,
        resolve_operation_lease: ResolveOperationLease,
        session_factory: LocalExternalBootSessionFactory,
        capacity_bytes: int,
        teardown_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if capacity_bytes <= 0:
            raise ValueError("external-boot capacity must be positive")
        self._recovery_root = recovery_root
        self._materializer = materializer
        self._recovery_writer = recovery_writer
        self._resolve_operation_lease = resolve_operation_lease
        self._session_factory = session_factory
        self._capacity_bytes = capacity_bytes
        self._teardown_clock = teardown_clock

    @contextmanager
    def open(
        self,
        authority: OpaqueProviderRef,
        expected: ExpectedOperationOwnership,
        *,
        local_timing: LocalExternalBootTimingV1 | None = None,
    ) -> Generator[LocalExternalBootOperation]:
        lease = self._resolve_operation_lease(authority)
        session = (
            self._session_factory.open(lease, expected)
            if local_timing is None
            else self._session_factory.open(lease, expected, local_timing=local_timing)
        )
        operation = _RealLocalExternalBootOperation(
            self._recovery_root,
            self._materializer,
            self._recovery_writer,
            session,
            self._capacity_bytes,
        )
        try:
            yield operation
        except BaseException as primary:
            try:
                session.close()
            except BaseException as close_error:
                primary.add_note(f"cleanup failed: {close_error!r}")
            raise
        else:
            session.close()

    @contextmanager
    def open_pinned(
        self, authority: OpaqueProviderRef, expected: ExpectedOperationOwnership
    ) -> Generator[None]:
        pin = self._session_factory.open_pinned(self._resolve_operation_lease(authority), expected)
        try:
            yield
        except BaseException as primary:
            try:
                pin.close()
            except BaseException as close_error:
                primary.add_note(f"cleanup failed: {close_error!r}")
            raise
        else:
            pin.close()

    def finalize_tombstone(self, recovery: RecoveryPoint, proof: FinalizeCleanupProof) -> None:
        with RecoveryMetadataStore(self._recovery_root) as store:
            store.finalize_tombstone(recovery.recovery_ref, recovery, proof)

    def record_cleanup_quarantine(
        self, recovery: RecoveryPoint, proof: FinalizeCleanupProof
    ) -> None:
        with RecoveryMetadataStore(self._recovery_root) as store:
            store.record_cleanup_quarantine(recovery, proof)

    def read_cleanup_quarantine(
        self, binding: ExternalBootActivationBinding
    ) -> CleanupQuarantineReceiptV1 | None:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.read_cleanup_quarantine(binding)

    def adopt_cleanup_quarantine(self, receipt: CleanupQuarantineReceiptV1) -> None:
        with RecoveryMetadataStore(self._recovery_root) as store:
            store.adopt_cleanup_quarantine(receipt)

    def observe_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.observe_preparation(request)

    def publish_preparation(
        self, receipt: ExternalBootPreparationObservation
    ) -> ExternalBootPreparationObservation:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.publish_preparation(receipt)

    def adopt_preparation(
        self,
        request: ExternalBootPreparationRequest,
        predecessor: ExternalBootPreparationRequest,
        predecessor_receipt_identity: str,
    ) -> ExternalBootPreparationObservation:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.adopt_preparation(request, predecessor, predecessor_receipt_identity)

    def preparation_materialization(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootMaterialization:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.preparation_materialization(request)

    def begin_system_teardown(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> AuthoritySystemTeardownFacts:
        with self._open_system_teardown(intent, authority) as session:
            inspection = session.inspect()
            with RecoveryMetadataStore(self._recovery_root) as store:
                store.begin_system_teardown(intent, inspection)
                recovery_absent = store.exact_recovery_absence(
                    intent.binding, adopted_receipt_absent=True
                )
            return _system_teardown_facts(
                intent, inspection, recovery_absent, None, intent.reservation
            )

    def observe_system_teardown(
        self, anchor: _LocalSystemTeardownFields, authority: OpaqueProviderRef
    ) -> AuthoritySystemTeardownFacts:
        with self._open_system_teardown(anchor, authority) as session:
            inspection = session.inspect()
            with RecoveryMetadataStore(self._recovery_root) as store:
                retained = store.read_system_teardown(anchor.binding)
                if retained is not None and not retained.intent.matches_anchor(anchor):
                    if (
                        not retained.intent.anchor_subject_matches(anchor)
                        or retained.intent.generation == anchor.generation
                    ):
                        raise ValueError(
                            "System teardown observation conflicts with retained intent"
                        )
                    if retained.intent.generation > anchor.generation:
                        raise SystemTeardownSupersededError
                    # ADR-0620 (#2921): this generation never reached `begin`; the earlier
                    # generation's record is not its own, so it observes as if absent.
                    retained = None
                recovery_absent = store.exact_recovery_absence(
                    anchor.binding, adopted_receipt_absent=True
                )
            completed_at = (
                retained.completed_at
                if retained is not None and retained.phase == "complete"
                else None
            )
            owner = retained.intent if retained is not None else anchor
            reservation = retained.intent.reservation if retained is not None else None
            return _system_teardown_facts(
                owner, inspection, recovery_absent, completed_at, reservation
            )

    def system_teardown_recovery_point(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> RecoveryPoint | None:
        """Resolve private recovery evidence without requiring a still-defined domain."""
        with self._open_system_teardown(intent, authority):
            reference = _recovery_ref(intent.binding)
            with RecoveryMetadataStore(self._recovery_root) as store:
                try:
                    metadata = store.reopen(reference, intent.binding)
                except FileNotFoundError:
                    try:
                        tombstone = store.reopen_tombstone(reference, intent.binding)
                    except FileNotFoundError:
                        return None
                    point = tombstone.recovery_point
                else:
                    point = RecoveryPoint(
                        binding=metadata.binding,
                        plan_identity=metadata.plan_identity,
                        materialization_identity=metadata.materialization_identity,
                        recovery_ref=reference,
                        source_state=metadata.source_state,
                        target_state=metadata.target_state,
                    )
        if point.binding != intent.binding or point.plan_identity != intent.plan_identity:
            raise ProviderRecoveryRefusal(
                "System teardown recovery point does not match retained intent"
            )
        return point

    def system_teardown_recovery_is_absent(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> bool:
        """Observe exact recovery absence without opening or creating activation state."""
        with (
            self._open_system_teardown(intent, authority),
            RecoveryMetadataStore(self._recovery_root) as store,
        ):
            return store.exact_recovery_absence(intent.binding, adopted_receipt_absent=True)

    def discard_system_teardown_recovery(
        self,
        intent: LocalSystemTeardownIntentV1,
        recovery: RecoveryPoint,
        proof: FinalizeCleanupProof,
        authority: OpaqueProviderRef,
    ) -> None:
        """Settle a retained recovery point without recovering it (#3341).

        The domain is destroyed next, so restoring the guest is pointless, and a conflicted
        activation cannot be restored: its definition matches neither source nor target. This
        needs the pinned teardown lane (so a foreign definition still refuses in the ownership
        check) but not a domain, and it accepts any recovery phase.
        """
        with (
            self._open_system_teardown(intent, authority) as session,
            RecoveryMetadataStore(self._recovery_root) as store,
        ):
            session.inspect()
            reference = recovery.recovery_ref
            if proof.binding != recovery.binding or proof.point_digest != recovery_point_digest(
                recovery
            ):
                raise ValueError("external-boot cleanup proof does not match recovery point")
            standing = store.read_cleanup_quarantine(recovery.binding)
            if standing is not None and standing.managed:
                # An adopted receipt is the operator's; teardown keeps it (ADR-0787, #3467).
                if standing.tombstone.recovery_point != recovery:
                    raise ValueError("adopted cleanup receipt does not match recovery point")
                store.drop_tombstone_residue(recovery)
                return
            if not store.cleanup_complete(reference, recovery):
                metadata = store.reopen(reference, recovery.binding)
                if _recovery_point_of(metadata, reference) != recovery:
                    raise ValueError(
                        "external-boot recovery metadata does not match recovery point"
                    )
                session.cleanup_payloads(metadata)
                store.publish_tombstone(
                    reference,
                    recovery.binding,
                    metadata,
                    recovery_point_digest(recovery),
                    require_recovered=False,
                )
            if standing is None:
                store.record_cleanup_quarantine(recovery, proof)
            store.finalize_tombstone(reference, recovery, proof)

    def teardown_system(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> AuthoritySystemTeardownFacts:
        with self._open_system_teardown(intent, authority) as session:
            inspection = session.inspect()
            with RecoveryMetadataStore(self._recovery_root) as store:
                record = store.begin_system_teardown(intent, inspection)
                if not store.exact_recovery_absence(intent.binding, adopted_receipt_absent=True):
                    return _system_teardown_facts(
                        intent, inspection, False, None, intent.reservation
                    )
            phases: tuple[tuple[SystemTeardownPhase, Callable[[], None]], ...] = (
                ("domain-destroyed", session.destroy),
                ("domain-undefined", session.undefine),
                ("overlay-removed", session.remove_overlay),
                ("baseline-removed", session.remove_baseline),
            )
            phase_names = ["intent-recorded", *(phase for phase, _action in phases), "complete"]
            for phase, action in phases[phase_names.index(record.phase) :]:
                action()
                with RecoveryMetadataStore(self._recovery_root) as store:
                    record = store.record_system_teardown_phase(record, phase)
            final = session.inspect()
            with RecoveryMetadataStore(self._recovery_root) as store:
                recovery_absent = store.exact_recovery_absence(
                    intent.binding, adopted_receipt_absent=True
                )
                physically_absent = (
                    final.domain_absent
                    and final.overlay_absent
                    and final.baseline_absent
                    and recovery_absent
                )
                if physically_absent and record.phase != "complete":
                    record = store.record_system_teardown_phase(
                        record, "complete", completed_at=self._teardown_clock()
                    )
                facts = _system_teardown_facts(
                    intent,
                    final,
                    recovery_absent,
                    record.completed_at,
                    intent.reservation,
                )
            return facts

    def stranded_facts(
        self, binding: ExternalBootActivationBinding, domain_name: str
    ) -> StrandedAuthorityFacts:
        """Read this authority's daemon and store with no session: there is no domain to open.

        `domain_name` comes from `systems.domain_name`, which the worker role can write, so the
        name this provider derives is checked too. A rewritten row cannot hide the real domain.
        """
        with RecoveryMetadataStore(self._recovery_root) as store:
            store_absent = store.exact_recovery_absence(binding)
        names = {domain_name, domain_name_for(UUID(binding.system_id))}
        return StrandedAuthorityFacts(
            domain_absent=not any(map(self._session_factory.domain_is_defined, names)),
            store_absent=store_absent,
            recovery_ref=_recovery_ref(binding).ref,
        )

    @contextmanager
    def _open_system_teardown(
        self, intent: _LocalSystemTeardownFields, authority: OpaqueProviderRef
    ) -> Generator[LocalSystemTeardownSession]:
        lease = self._resolve_operation_lease(authority)
        session = self._session_factory.open_teardown(lease, _expected_binding(intent.binding))
        try:
            yield session
        except BaseException as primary:
            try:
                session.close()
            except BaseException as close_error:
                primary.add_note(f"cleanup failed: {close_error!r}")
            raise
        else:
            session.close()


def _recovery_point_of(
    metadata: LocalRecoveryMetadataV1, reference: OpaqueProviderRef
) -> RecoveryPoint:
    return RecoveryPoint(
        binding=metadata.binding,
        plan_identity=metadata.plan_identity,
        materialization_identity=metadata.materialization_identity,
        recovery_ref=reference,
        source_state=metadata.source_state,
        target_state=metadata.target_state,
    )


def _system_teardown_facts(
    intent: _LocalSystemTeardownFields,
    inspection: LocalSystemTeardownInspection,
    recovery_absent: bool,
    completed_at: datetime | None,
    reservation: AuthorityTeardownReservationV1 | None = None,
) -> AuthoritySystemTeardownFacts:
    physically_absent = (
        inspection.domain_absent
        and inspection.overlay_absent
        and inspection.baseline_absent
        and recovery_absent
    )
    return AuthoritySystemTeardownFacts(
        intent_identity=intent.identity,
        domain_absent=inspection.domain_absent,
        overlay_absent=inspection.overlay_absent,
        baseline_absent=inspection.baseline_absent,
        recovery_absent=recovery_absent,
        quarantine_retained=not physically_absent or completed_at is None,
        completed_at=completed_at,
        reservation=reservation,
    )


class _RealLocalExternalBootOperation:
    def __init__(
        self,
        recovery_root: Path,
        materializer: LocalExternalBootMaterializer,
        recovery_writer: GuestRecoveryWriter,
        session: LocalExternalBootSession,
        capacity_bytes: int,
    ) -> None:
        self._recovery_root = recovery_root
        self._materializer = materializer
        self._recovery_writer = recovery_writer
        self._session = session
        self._capacity_bytes = capacity_bytes

    def materialize(self, plan: ExternalBootPlan) -> ExternalBootMaterialization:
        reservation = (
            materialization_reservation_bytes(plan)
            + _MAX_PROJECTION_BYTES
            + _MAX_RECOVERY_METADATA_BYTES
        )
        if reservation > self._capacity_bytes:
            raise ValueError("external-boot materialization exceeds configured capacity")
        return self._materializer.materialize(plan, self._session)

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
    ) -> LocalRecoveryMetadataV1:
        reference = _recovery_ref(binding)
        with RecoveryMetadataStore(self._recovery_root) as store:
            try:
                complete = store.reopen(reference, binding)
            except FileNotFoundError:
                complete = None
            if complete is not None:
                _validate_preparation_owner(complete, materialization, binding)
                return complete
            try:
                intent = store.reopen_pre_stop(reference, binding)
            except FileNotFoundError:
                inspection = self._session.inspect_closed()
                intent = self._materializer.inspect_prepare(
                    materialization, binding, inspection, self._session
                )
                _validate_preparation_owner(intent, materialization, binding)
                _validate_preparation_inspection(intent, inspection, retry=False)
                store.publish_pre_stop(intent)
            else:
                _validate_preparation_owner(intent, materialization, binding)
                _validate_preparation_inspection(intent, self._session.inspect_closed(), retry=True)
        self._session.stop_and_require_inactive(mode="clean")
        with RecoveryMetadataStore(self._recovery_root) as store:
            owned_sink = store.recovery_archive_sink(reference, intent)
        projection = self._session.reopen_projection(materialization.artifacts.kernel)
        primary: BaseException | None = None
        try:
            with self._session.guest() as guest:
                _require_whole_disk_root(guest, projection)
                tree = LibguestfsAuthenticatedGuestTree(
                    guest,
                    binding=binding,
                    release=intent.release,
                    root=f"/lib/modules/{intent.release}",
                    mutable=False,
                )
                capture_sink, owned_sink = owned_sink, None
                capture = self._recovery_writer.capture(tree, intent.release, capture_sink)
                debuginfo = None
                if materialization.artifacts.debuginfo is not None:
                    debuginfo = (
                        GuestDebuginfoFile(
                            guest, binding=binding, release=intent.release
                        ).observe_live(),
                        self._materialized_debuginfo_state(materialization),
                    )
                with guest_policy(guest) as policy:
                    target_manifest = materialization.installed_module_tree
                    if policy is not None:
                        descriptor = self._session.open_projection_artifact(
                            materialization.artifacts.modules, os.O_RDONLY
                        )
                        try:
                            target_manifest = _prepared_module_manifest(
                                descriptor,
                                intent.release,
                                materialization.installed_module_tree,
                                policy,
                            )
                        finally:
                            os.close(descriptor)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if owned_sink is not None:
                try:
                    owned_sink.close()
                except BaseException as cleanup:
                    if primary is None:
                        raise
                    primary.add_note(f"recovery archive sink cleanup failed: {cleanup!r}")
        metadata = _complete_preparation_metadata(
            intent, materialization, capture, target_manifest, debuginfo
        )
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.complete_preparation(reference, intent, metadata)

    def _materialized_debuginfo_state(
        self, materialization: ExternalBootMaterialization
    ) -> PresentComponentState:
        """The identity the staged vmlinux must read back as in the guest (#3130)."""
        if materialization.artifacts.debuginfo is None:
            raise ValueError("external-boot materialization has no debuginfo payload")
        descriptor = self._session.open_projection_artifact(
            materialization.artifacts.debuginfo, os.O_RDONLY
        )
        try:
            digest, size = _open_descriptor_digest(descriptor)
        finally:
            os.close(descriptor)
        if digest != materialization.verified_debuginfo_sha256:
            raise ValueError("materialized debuginfo bytes do not match materialization")
        return debuginfo_file_state(size=size, sha256=digest, mode=0o644, uid=0, gid=0)

    @staticmethod
    def recovery_ref(binding: ExternalBootActivationBinding) -> OpaqueProviderRef:
        return _recovery_ref(binding)

    def reopen(self, recovery: RecoveryPoint) -> LocalRecoveryMetadataV1:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.reopen(recovery.recovery_ref, recovery.binding)

    def reopen_binding(self, binding: ExternalBootActivationBinding) -> LocalRecoveryMetadataV1:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.reopen(_recovery_ref(binding), binding)

    def reopen_cleanup_tombstone(
        self, binding: ExternalBootActivationBinding
    ) -> CleanupTombstoneV1:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.reopen_tombstone(_recovery_ref(binding), binding)

    def observe_state(self, metadata: LocalRecoveryMetadataV1) -> LocalObservedState:
        # Each half is read independently so a readable definition still classifies when
        # the guest tree is not reachable. A failed read becomes ``None`` rather than an
        # exception: the caller must be able to name "unreadable" as an outcome, and no
        # libvirt or libguestfs message may cross this boundary (ADR-0584).
        try:
            inspection = self._session.inspect_closed(projected=True)
        except Exception:  # noqa: BLE001 - an unreadable definition is a classification
            return LocalObservedState(definition=None, modules=None, active=None)
        modules: ComponentState | None = metadata.inactive_modules
        if not inspection.active:
            try:
                with self._session.guest() as opened_guest:
                    modules = self._observe_modules(opened_guest, metadata)
            except Exception:  # noqa: BLE001 - an unreadable module tree is a classification
                modules = None
        return LocalObservedState(
            definition=inspection.source_boot_identity,
            modules=modules,
            active=inspection.active,
        )

    def _observe_modules(
        self, guest: InactiveGuest, metadata: LocalRecoveryMetadataV1
    ) -> ComponentState:
        tree = LibguestfsAuthenticatedGuestTree(
            cast(_GuestfsTreeHandle, guest),
            binding=metadata.binding,
            release=metadata.release,
            root=f"/lib/modules/{metadata.release}",
            mutable=False,
        )
        return self._recovery_writer.observe(tree, metadata.release)

    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        if self._host_state(metadata) != ("source", False):
            raise ValueError("external-boot module activation requires inactive source XML/power")
        desired = _present_component(metadata.target_state.modules, "target module state")
        prior = _layout_component(metadata.source_state.modules)
        with self._session.guest() as opened_guest:
            guest = cast(_GuestfsTreeHandle, opened_guest)
            policy_context = (
                guest_policy(opened_guest)
                if metadata.phase == "pre-stop-intent"
                else nullcontext(None)
            )
            with policy_context as policy:
                publication = _SessionModulePublicationIO(
                    guest,
                    metadata,
                    self._recovery_root,
                    self._recovery_writer,
                    self._session,
                    label_policy=policy,
                )
                if metadata.phase == "pre-stop-intent":
                    before = ModuleLayout(prior, None, None)
                    staged = ModuleLayout(prior, desired, None)
                    layout = publication.observe_layout()
                    if layout == before:
                        source = self._kernel_bundle_source(metadata)
                        try:
                            publication.create_staging()
                            self._recovery_writer.install(
                                publication.staging_tree(), metadata.release, source
                            )
                        finally:
                            source.close()
                        if publication.observe_layout() != staged:
                            raise ValueError(
                                "external-boot staged target modules do not match metadata"
                            )
                    elif layout != staged:
                        raise ValueError(
                            "external-boot target staging layout conflicts with metadata"
                        )
                    # Staged before any module move is recorded, so a failed upload leaves
                    # the record in pre-stop-intent, which recovery settles (#3130).
                    if (states := _debuginfo_states(metadata)) is not None:
                        _debuginfo_file(opened_guest, metadata).stage(
                            *states,
                            lambda path: opened_guest.upload_projection_artifact(
                                _debuginfo_ref(metadata), path
                            ),
                            size=lambda: self._session_artifact_size(_debuginfo_ref(metadata)),
                            system_id=metadata.binding.system_id,
                        )
                    publication.guest_sync()
                    publication.record_phase(
                        PublicationPhase.MOVE_READY
                        if prior is not None
                        else PublicationPhase.OLD_ASIDE
                    )
                self._finish_present_publication(publication, prior=prior, desired=desired)
                if (states := _debuginfo_states(metadata)) is not None:
                    _debuginfo_file(opened_guest, metadata).publish(*states)
                completed = publication.metadata
                observed = self._observe_modules(opened_guest, completed)
        self.record_phase(completed, "module-restored", inactive_modules=observed)

    def _session_artifact_size(self, artifact: OpaqueProviderRef) -> int:
        descriptor = self._session.open_projection_artifact(artifact, os.O_RDONLY)
        try:
            return os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)

    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None:
        while metadata.phase == "module-restored":
            xml, active = self._host_state(metadata)
            if xml == "source" and not active:
                self._session.define_xml(metadata.target_xml, projected=True)
                continue
            if xml == "target" and not active and metadata.prior_power == "inactive":
                self.record_phase(metadata, "target-defined")
                return
            if xml == "target" and not active and metadata.prior_power == "running":
                self._session.start()
                continue
            if xml == "target" and active and metadata.prior_power == "running":
                self._require_readiness()
                if self._host_state(metadata) != ("target", True):
                    raise ValueError("external-boot target changed during readiness")
                self.record_phase(metadata, "target-defined")
                return
            raise ValueError(
                "external-boot target XML/power state conflicts with recovery metadata"
            )

    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation:
        if self._host_state(metadata) != ("target", True):
            raise ValueError("external-boot observation requires exact running target XML/power")
        observed = self._session.observe_running()
        if observed.identity != metadata.expected_running:
            raise ValueError("external-boot running kernel does not match recovery metadata")
        return observed

    def recover_modules(
        self, metadata: LocalRecoveryMetadataV1, acknowledged: LocalObservedState | None = None
    ) -> None:
        if metadata.phase == "pre-stop-intent":
            self._settle_unpublished_modules(metadata)
            return
        self._stop_for_recovery(metadata, acknowledged)
        # The tree being replaced is the recorded target, or the acknowledged third tree: its
        # manifest is the compare-and-swap expectation the live layout is checked against.
        replaced = (
            metadata.target_state.modules
            if acknowledged is None or acknowledged.modules is None
            else acknowledged.modules
        )
        if acknowledged is not None and replaced == metadata.source_state.modules:
            self._confirm_modules_restored(metadata, replaced)
            return
        target = _layout_component(replaced)
        desired = _layout_component(metadata.source_state.modules)
        with self._session.guest() as opened_guest:
            _restore_debuginfo(opened_guest, metadata)
            guest = cast(_GuestfsTreeHandle, opened_guest)
            publication = _SessionModulePublicationIO(
                guest,
                metadata,
                self._recovery_root,
                self._recovery_writer,
                self._session,
            )
            if metadata.phase in {"target-defined", "module-restored"}:
                terminal = ModuleLayout(desired, None, None)
                layout = publication.observe_layout()
                if metadata.phase == "module-restored" and layout == terminal:
                    return
                if isinstance(metadata.capture, AbsentModuleCapture):
                    if layout != ModuleLayout(target, None, None):
                        raise ValueError(
                            "external-boot absence recovery layout conflicts with metadata"
                        )
                    publication.guest_sync()
                    publication.record_phase(PublicationPhase.MOVE_READY)
                else:
                    staged = ModuleLayout(target, desired, None)
                    if layout == ModuleLayout(target, None, None):
                        source = self._recovery_archive_source(metadata)
                        try:
                            manifest = self._recovery_writer.restore(
                                publication.staging_tree(),
                                metadata.release,
                                metadata.capture,
                                source,
                            )
                        finally:
                            source.close()
                        expected = _present_component(
                            metadata.source_state.modules, "source module state"
                        )
                        if manifest != expected.manifest:
                            raise ValueError(
                                "external-boot staged recovery modules do not match metadata"
                            )
                    elif layout != staged:
                        raise ValueError(
                            "external-boot module recovery staging conflicts with metadata"
                        )
                    if publication.observe_layout() != staged:
                        raise ValueError(
                            "external-boot staged recovery modules changed before publication"
                        )
                    publication.guest_sync()
                    publication.record_phase(PublicationPhase.MOVE_READY)
            if isinstance(metadata.capture, AbsentModuleCapture):
                self._finish_absence_publication(
                    publication, prior=_present_component(replaced, "replaced module state")
                )
            else:
                assert desired is not None
                self._finish_present_publication(publication, prior=target, desired=desired)
            completed = publication.metadata
            observed = self._observe_modules(opened_guest, completed)
        self.record_phase(completed, "module-restored", inactive_modules=observed)

    def _confirm_modules_restored(
        self, metadata: LocalRecoveryMetadataV1, expected: ComponentState
    ) -> None:
        """Record the module tree as restored when the live tree is already the source."""
        with self._session.guest() as opened_guest:
            observed = self._observe_modules(opened_guest, metadata)
        if observed != expected:
            raise ValueError("external-boot module tree changed since the acknowledged observation")
        self.record_phase(metadata, "module-restored", inactive_modules=observed)

    def _settle_unpublished_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        """ADR-0707: an activation that never published keeps only its own staging name."""
        if self._host_state(metadata) != ("source", False):
            raise ProviderRecoveryRefusal(
                "external-boot pre-stop recovery requires inactive source XML/power"
            )
        prior = _layout_component(metadata.source_state.modules)
        with self._session.guest() as opened_guest:
            _restore_debuginfo(opened_guest, metadata)
            publication = _SessionModulePublicationIO(
                cast(_GuestfsTreeHandle, opened_guest),
                metadata,
                self._recovery_root,
                self._recovery_writer,
                self._session,
            )
            publication.discard_staging()
            if publication.observe_layout() != ModuleLayout(prior, None, None):
                raise ProviderRecoveryRefusal(
                    "external-boot pre-stop module layout conflicts with metadata"
                )
            observed = self._observe_modules(opened_guest, metadata)
        self.record_phase(metadata, "module-restored", inactive_modules=observed)

    def define_source(
        self, metadata: LocalRecoveryMetadataV1, acknowledged: LocalObservedState | None = None
    ) -> None:
        while metadata.phase == "module-restored":
            xml, active = self._host_state(metadata, acknowledged)
            if xml in {"target", "acknowledged"} and not active:
                self._session.define_xml(metadata.source_xml)
                continue
            if xml == "source" and not active:
                self.record_phase(metadata, "source-restored")
                return
            raise ValueError(
                "external-boot source XML/power state conflicts with recovery metadata"
            )

    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None:
        while metadata.phase == "source-restored":
            xml, active = self._host_state(metadata)
            if xml == "source" and not active and metadata.prior_power == "inactive":
                self.record_phase(metadata, "recovered")
                return
            if xml == "source" and not active and metadata.prior_power == "running":
                self._session.start()
                continue
            if xml == "source" and active and metadata.prior_power == "running":
                self._require_readiness()
                if self._host_state(metadata) != ("source", True):
                    raise ValueError("external-boot source changed during readiness")
                self.record_phase(metadata, "recovered")
                return
            raise ValueError("external-boot restored power state conflicts with recovery metadata")

    def record_phase(
        self,
        metadata: LocalRecoveryMetadataV1,
        phase: RecoveryPhase,
        *,
        inactive_modules: ComponentState | None = None,
    ) -> LocalRecoveryMetadataV1:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.record_phase(
                _recovery_ref(metadata.binding),
                metadata.binding,
                metadata,
                phase,
                inactive_modules=inactive_modules,
            )

    def cleanup_complete(self, recovery: RecoveryPoint) -> bool:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.cleanup_complete(recovery.recovery_ref, recovery)

    def cleanup(self, metadata: LocalRecoveryMetadataV1, point_digest: Digest) -> None:
        with RecoveryMetadataStore(self._recovery_root) as store:
            reference = _recovery_ref(metadata.binding)
            if store.reopen(reference, metadata.binding) != metadata:
                raise ValueError("recovery metadata changed before cleanup")
            self._session.cleanup_payloads(metadata)
            store.publish_tombstone(reference, metadata.binding, metadata, point_digest)

    def abort_preparation(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: Digest,
        source_identity: str,
        target_identity: str,
        authority: OpaqueProviderRef,
    ) -> PartialAbortResult:
        result = self._abort_preparation(
            binding,
            plan_identity,
            authority,
            expected_identities=(source_identity, target_identity),
            restore_power=True,
        )
        if result in {"removed", "absent"}:
            with RecoveryMetadataStore(self._recovery_root) as store:
                store.prune_empty_activation_parents(binding)
        return result

    def abort_system_teardown_preparation(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: Digest,
        authority: OpaqueProviderRef,
    ) -> PartialAbortResult:
        # The System is being torn down, so its prior running power is not restored (#2898).
        result = self._abort_preparation(
            binding, plan_identity, authority, expected_identities=None, restore_power=False
        )
        if result in {"removed", "absent"}:
            with RecoveryMetadataStore(self._recovery_root) as store:
                store.prune_empty_activation_parents(binding)
        return result

    def _abort_preparation(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: Digest,
        authority: OpaqueProviderRef,
        *,
        expected_identities: tuple[str, str] | None,
        restore_power: bool,
    ) -> PartialAbortResult:
        with RecoveryMetadataStore(self._recovery_root) as store:
            partial = store.inspect_abortable_partial(binding, plan_identity, authority)
            if isinstance(partial, str):
                return cast(PartialAbortResult, partial)
            intent = partial.intent
            if intent is not None:
                if (
                    expected_identities is not None
                    and (
                        intent.source_boot,
                        intent.target_boot,
                    )
                    != expected_identities
                ):
                    raise ValueError("recovery partial identity conflicts with teardown request")
                _validate_preparation_inspection(intent, self._session.inspect_closed(), retry=True)
                if restore_power and intent.prior_power == "running":
                    self._session.restore_power()
                    readiness = self._session.readiness()
                    if not readiness.ok:
                        raise ValueError("source readiness failed while aborting preparation")
            if partial.materialization is not None:
                store.remove_abortable_activation(binding, plan_identity, partial.materialization)
            store.remove_abortable_partial(binding, plan_identity, authority)
            return "removed"

    def recovery_is_absent(self, binding: ExternalBootActivationBinding) -> bool:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.exact_recovery_absence(binding)

    def _kernel_bundle_source(self, metadata: LocalRecoveryMetadataV1) -> KernelBundleSource:
        descriptor = self._session.open_projection_artifact(
            metadata.materialized_modules, os.O_RDONLY
        )
        try:
            return KernelBundleSource(
                descriptor,
                binding=metadata.binding,
                release=metadata.release,
                size=metadata.materialized_modules_bytes,
                digest=metadata.materialized_modules_sha256,
            )
        finally:
            os.close(descriptor)

    def _recovery_archive_source(
        self,
        metadata: LocalRecoveryMetadataV1,
    ) -> RecoveryArchiveSource:
        if not isinstance(metadata.capture, ModuleArchiveCapture):
            raise ValueError("absent module capture has no recovery archive")
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.recovery_archive_source(
                _recovery_ref(metadata.binding),
                metadata,
            )

    @staticmethod
    def _finish_present_publication(
        publication: _SessionModulePublicationIO,
        *,
        prior: ComponentState | None,
        desired: PresentComponentState,
    ) -> None:
        while publication.metadata.phase != PublicationPhase.PUBLICATION_COMPLETE:
            phase = PublicationPhase(publication.metadata.phase)
            advance_module_publication(
                publication,
                phase=phase,
                layout=publication.observe_layout(),
                prior=prior,
                desired=desired,
            )
        if publication.observe_layout() != ModuleLayout(desired, None, None):
            raise ValueError("external-boot completed module publication layout conflicts")

    @staticmethod
    def _finish_absence_publication(
        publication: _SessionModulePublicationIO,
        *,
        prior: PresentComponentState,
    ) -> None:
        while publication.metadata.phase != PublicationPhase.ABSENCE_CLEANED:
            phase = PublicationPhase(publication.metadata.phase)
            advance_absence_publication(
                publication,
                phase=phase,
                layout=publication.observe_layout(),
                prior=prior,
            )
        if publication.observe_layout() != ModuleLayout(None, None, None):
            raise ValueError("external-boot completed module absence layout conflicts")

    def _host_state(
        self, metadata: LocalRecoveryMetadataV1, acknowledged: LocalObservedState | None = None
    ) -> tuple[Literal["source", "target", "acknowledged"], bool]:
        """Classify the domain definition; a third one only when it is the acknowledged one."""
        inspection = self._session.inspect_closed(projected=True)
        if inspection.xml == metadata.source_xml.encode():
            return "source", inspection.active
        if _same_domain_definition(inspection.xml.decode(), metadata.target_xml):
            return "target", inspection.active
        if acknowledged is not None and acknowledged.definition == inspection.source_boot_identity:
            return "acknowledged", inspection.active
        raise ValueError("external-boot observed domain XML does not match recovery metadata")

    def _stop_for_recovery(
        self, metadata: LocalRecoveryMetadataV1, acknowledged: LocalObservedState | None = None
    ) -> None:
        xml, active = self._host_state(metadata, acknowledged)
        if metadata.phase == "target-defined" and xml == "source":
            raise ValueError("external-boot recovery expected the target domain XML")
        if active and xml == "source":
            raise ValueError("external-boot refuses to stop an unexpected running domain")
        if not active:
            self._session.require_inactive()
            return
        # ADR-0681: only a target that reached `target-defined` stops cleanly, and only on KVM.
        # An acknowledged third definition is not that target, so it is destroyed.
        mode: StopMode = (
            "clean-on-kvm" if metadata.phase == "target-defined" and xml == "target" else "destroy"
        )
        try:
            self._session.stop_and_require_inactive(mode=mode)
        except Exception as primary:
            try:
                after = self._host_state(metadata, acknowledged)
            except Exception as observation_error:
                primary.add_note(f"post-stop host-state observation failed: {observation_error!r}")
                raise primary from None
            if after == (xml, False):
                self._session.require_inactive()
                return
            raise
        if self._host_state(metadata, acknowledged) != (xml, False):
            raise ValueError("external-boot stop did not preserve exact XML and inactivity")

    def _require_readiness(self) -> None:
        if self._session.readiness() != ReadinessResult(True, True, None):
            raise ValueError("external-boot readiness did not answer with exact success")


class _SessionModulePublicationIO:
    """Session-owned adapter for one exact three-name module publication."""

    def __init__(
        self,
        guest: _GuestfsTreeHandle,
        metadata: LocalRecoveryMetadataV1,
        recovery_root: Path,
        writer: GuestRecoveryWriter,
        session: LocalExternalBootSession,
        *,
        label_policy: ModuleLabelPolicy | None = None,
    ) -> None:
        self._guest = guest
        self.metadata = metadata
        self._recovery_root = recovery_root
        self._writer = writer
        self._session = session
        self._label_policy = label_policy
        base = f"/lib/modules/.kdive-{metadata.binding.activation_id}"
        self._live = f"/lib/modules/{metadata.release}"
        self._staging = f"{base}-staging"
        self._old = f"{base}-old"

    def require_inactive(self) -> None:
        self._session.require_inactive()

    def observe_layout(self) -> ModuleLayout:
        return ModuleLayout(
            self._observe(self._live),
            self._observe(self._staging),
            self._observe(self._old),
        )

    def create_staging(self) -> None:
        self._guest.mkdir(self._staging)

    def staging_tree(self) -> LibguestfsAuthenticatedGuestTree:
        return LibguestfsAuthenticatedGuestTree(
            self._guest,
            binding=self.metadata.binding,
            release=self.metadata.release,
            root=self._staging,
            mutable=True,
            label_policy=self._label_policy,
        )

    def move_live_to_old(self) -> None:
        self._guest.mv(self._live, self._old)

    def move_staging_to_live(self) -> None:
        self._guest.mv(self._staging, self._live)

    def move_old_to_live(self) -> None:
        self._guest.mv(self._old, self._live)

    def remove_old(self) -> None:
        self._guest.rm_rf(self._old)

    def discard_staging(self) -> None:
        if self._guest.exists(self._staging):
            self._guest.rm_rf(self._staging)
            self._guest.sync()

    def guest_sync(self) -> None:
        self._guest.sync()

    def record_phase(self, phase: PublicationPhase) -> None:
        with RecoveryMetadataStore(self._recovery_root) as store:
            self.metadata = store.record_phase(
                _recovery_ref(self.metadata.binding),
                self.metadata.binding,
                self.metadata,
                cast(RecoveryPhase, phase.value),
            )

    def _observe(self, root: str) -> ComponentState | None:
        tree = LibguestfsAuthenticatedGuestTree(
            self._guest,
            binding=self.metadata.binding,
            release=self.metadata.release,
            root=root,
            mutable=False,
        )
        kind = tree.root_kind()
        if kind == "absent":
            return None
        if kind != "directory":
            raise ValueError("external-boot module publication name is not a directory")
        return self._writer.observe(tree, self.metadata.release)


_DEBUGINFO_ROOT = "/usr/lib/debug/lib/modules"
_DEBUGINFO_ANCESTORS = ("/usr/lib/debug", "/usr/lib/debug/lib", _DEBUGINFO_ROOT)
type _DebuginfoLayout = tuple[
    PresentComponentState | None, PresentComponentState | None, PresentComponentState | None
]


def debuginfo_file_state(
    *, size: int, sha256: str, mode: int, uid: int, gid: int
) -> PresentComponentState:
    """Identity of one regular guest file: its content plus the metadata publish sets (#3130)."""
    facts = {"gid": gid, "mode": f"{mode:04o}", "sha256": sha256, "size": size, "uid": uid}
    data = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(b"kdive-debuginfo-file-v1\0" + data).hexdigest()
    return PresentComponentState(manifest=f"sha256:{digest}")


class GuestDebuginfoFile:
    """Activation-owned live, staging and old names for one release's DWARF vmlinux (#3130).

    Every step is chosen from the observed (live, staging, old) layout and the known prior and
    target states, so a restart repeats or skips a step without a recorded phase. The prior
    file stays beside the live name under the old name until recovery renames it back.
    """

    def __init__(
        self, guest: InactiveGuest, *, binding: ExternalBootActivationBinding, release: str
    ) -> None:
        if not release or "/" in release or release in {".", ".."}:
            raise ValueError("debuginfo release is invalid")
        self._guest = guest
        self._directory = f"{_DEBUGINFO_ROOT}/{release}"
        prefix = f"{self._directory}/.kdive-{binding.activation_id}-vmlinux"
        self.live = f"{self._directory}/vmlinux"
        self.staging = f"{prefix}-staging"
        self.old = f"{prefix}-old"

    def observe_live(self) -> ComponentState:
        state = self._layout()[0]
        return AbsentComponentState() if state is None else state

    def stage(
        self,
        source: ComponentState,
        target: PresentComponentState,
        upload: Callable[[str], None],
        *,
        size: Callable[[], int],
        system_id: str,
    ) -> None:
        prior = _layout_component(source)
        live, staging, old = self._layout()
        if live != prior or old is not None:
            raise ValueError("external-boot debuginfo staging conflict")
        if staging == target:
            return
        if staging is not None:
            self._guest.rm_rf(self.staging)
        self._directories(create=True)
        require_free_space_for_vmlinux(
            self._guest.statvfs, self._directory, size=size(), target=system_id
        )
        upload(self.staging)
        self._guest.chmod(0o644, self.staging)
        self._guest.chown(0, 0, self.staging)
        if self._observe(self.staging) != target:
            raise ValueError("staged debuginfo does not match the materialized vmlinux")
        self._guest.sync()

    def publish(self, source: ComponentState, target: PresentComponentState) -> None:
        prior = _layout_component(source)
        layout = self._layout()
        if prior is not None and layout == (prior, target, None):
            self._guest.mv(self.live, self.old)
            layout = (None, target, prior)
        if layout == (None, target, prior):
            self._guest.mv(self.staging, self.live)
            self._guest.sync()
        elif layout != (target, None, prior):
            raise ValueError("external-boot debuginfo publication conflict")
        if self._layout() != (target, None, prior):
            raise ValueError("external-boot debuginfo publication did not complete")

    def restore(self, source: ComponentState, target: PresentComponentState) -> None:
        prior = _layout_component(source)
        layout = self._layout()
        if layout == (prior, None, None):
            return
        if layout in ((target, None, prior), (None, target, prior)):
            if prior is not None:
                self._guest.mv(self.old, self.live)
            elif layout[0] is not None:
                self._guest.rm_rf(self.live)
            layout = (prior, layout[1], None)
        if layout[0] != prior or layout[2] is not None:
            raise ValueError("external-boot debuginfo recovery conflict")
        if layout[1] is not None:
            self._guest.rm_rf(self.staging)
        self._guest.sync()
        if self._layout() != (prior, None, None):
            raise ValueError("external-boot debuginfo recovery did not complete")

    def _layout(self) -> _DebuginfoLayout:
        if not self._directories(create=False):
            return None, None, None
        return self._observe(self.live), self._observe(self.staging), self._observe(self.old)

    def _directories(self, *, create: bool) -> bool:
        # Distro symlinks above the release directory are followed (Fedora's `filesystem`
        # package ships /usr/lib/debug/lib -> usr/lib); libguestfs keeps every path inside the
        # guest root. The release directory itself must be a real directory.
        walk = [(path, True) for path in _DEBUGINFO_ANCESTORS] + [(self._directory, False)]
        for path, follow in walk:
            if not self._guest.exists(path):
                if not create:
                    return False
                self._guest.mkdir(path)
            elif not self._guest.is_dir(path, followsymlinks=follow):
                raise ValueError("external-boot debuginfo directory is not a directory")
        return True

    def _observe(self, path: str) -> PresentComponentState | None:
        # `exists` follows links, so a dangling link would otherwise read as absent.
        if self._guest.is_symlink(path):
            raise ValueError("external-boot debuginfo name is not a regular file")
        if not self._guest.exists(path):
            return None
        status = self._guest.lstatns(path)
        if not stat.S_ISREG(status["st_mode"]):
            raise ValueError("external-boot debuginfo name is not a regular file")
        return debuginfo_file_state(
            size=status["st_size"],
            sha256="sha256:" + self._guest.checksum("sha256", path),
            mode=stat.S_IMODE(status["st_mode"]),
            uid=status["st_uid"],
            gid=status["st_gid"],
        )


def _debuginfo_states(
    metadata: LocalRecoveryMetadataV1,
) -> tuple[ComponentState, PresentComponentState] | None:
    """The prior and target debuginfo states, or ``None`` for a plan without the member."""
    target = metadata.target_state.debuginfo
    if target is None:
        return None
    source = metadata.source_state.debuginfo
    if source is None or not isinstance(target, PresentComponentState):
        raise ValueError("external-boot debuginfo recovery state is incomplete")
    return source, target


def _debuginfo_file(guest: InactiveGuest, metadata: LocalRecoveryMetadataV1) -> GuestDebuginfoFile:
    return GuestDebuginfoFile(guest, binding=metadata.binding, release=metadata.release)


def _debuginfo_ref(metadata: LocalRecoveryMetadataV1) -> OpaqueProviderRef:
    """The `debuginfo` payload beside the materialized modules in the same projection."""
    directory = metadata.materialized_modules.ref.rsplit("/", 1)[0]
    return OpaqueProviderRef(ref=f"{directory}/debuginfo")


def _restore_debuginfo(guest: InactiveGuest, metadata: LocalRecoveryMetadataV1) -> None:
    if (states := _debuginfo_states(metadata)) is not None:
        _debuginfo_file(guest, metadata).restore(*states)


def _layout_component(state: ComponentState) -> PresentComponentState | None:
    return state if isinstance(state, PresentComponentState) else None


def _present_component(state: ComponentState, label: str) -> PresentComponentState:
    if not isinstance(state, PresentComponentState):
        raise ValueError(f"external-boot {label} must be present")
    return state


def _validate_preparation_owner(
    value: LocalPreStopIntentV1 | LocalRecoveryMetadataV1,
    materialization: ExternalBootMaterialization,
    binding: ExternalBootActivationBinding,
) -> None:
    if (
        value.binding != binding
        or value.materialization_identity != materialization.identity
        or value.plan_identity != materialization.plan_identity
        or value.expected_running != materialization.kernel_observation
    ):
        raise ValueError("pre-stop intent does not match activation")


def _complete_preparation_metadata(
    intent: LocalPreStopIntentV1,
    materialization: ExternalBootMaterialization,
    capture: ModuleCapture,
    target_manifest: str,
    debuginfo: tuple[ComponentState, PresentComponentState] | None = None,
) -> LocalRecoveryMetadataV1:
    source_modules: ComponentState
    if isinstance(capture, AbsentModuleCapture):
        source_modules = AbsentComponentState()
    else:
        source_modules = PresentComponentState(manifest=capture.manifest)
    return LocalRecoveryMetadataV1.model_validate(
        intent.model_dump(exclude={"schema_"}, by_alias=True)
        | {
            "source_state": ProviderStateIdentity(
                definition=intent.source_boot,
                modules=source_modules,
                debuginfo=None if debuginfo is None else debuginfo[0],
            ),
            "target_state": ProviderStateIdentity(
                definition=intent.target_boot,
                modules=PresentComponentState(manifest=target_manifest),
                debuginfo=None if debuginfo is None else debuginfo[1],
            ),
            "capture": capture,
            "phase": "pre-stop-intent",
        }
    )


def _validate_preparation_inspection(
    intent: LocalPreStopIntentV1,
    inspection: ClosedDomainInspection,
    *,
    retry: bool,
) -> None:
    expected_prior = "running" if inspection.active else "inactive"
    if (
        intent.source_xml.encode() != inspection.xml
        or intent.source_definition != inspection.definition_identity
        or intent.source_boot != inspection.source_boot_identity
        or (not retry and intent.prior_power != expected_prior)
    ):
        raise ValueError("closed domain inspection does not match pre-stop intent")


class LocalLibvirtExternalBoot:
    """Six-port local lifecycle coordinator over authenticated, injected host I/O."""

    def __init__(self, io: LocalExternalBootIO) -> None:
        self._io = io

    def begin_system_teardown(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> AuthoritySystemTeardownFacts:
        return cast(LocalSystemTeardownIO, self._io).begin_system_teardown(intent, authority)

    def teardown_system(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> AuthoritySystemTeardownFacts:
        return cast(LocalSystemTeardownIO, self._io).teardown_system(intent, authority)

    def observe_system_teardown(
        self, intent: _LocalSystemTeardownFields, authority: OpaqueProviderRef
    ) -> AuthoritySystemTeardownFacts:
        return cast(LocalSystemTeardownIO, self._io).observe_system_teardown(intent, authority)

    def system_teardown_recovery_point(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> RecoveryPoint | None:
        return cast(LocalSystemTeardownIO, self._io).system_teardown_recovery_point(
            intent, authority
        )

    def system_teardown_recovery_is_absent(
        self, intent: LocalSystemTeardownIntentV1, authority: OpaqueProviderRef
    ) -> bool:
        return cast(LocalSystemTeardownIO, self._io).system_teardown_recovery_is_absent(
            intent, authority
        )

    def discard_system_teardown_recovery(
        self,
        intent: LocalSystemTeardownIntentV1,
        recovery: RecoveryPoint,
        proof: FinalizeCleanupProof,
        authority: OpaqueProviderRef,
    ) -> None:
        cast(LocalSystemTeardownIO, self._io).discard_system_teardown_recovery(
            intent, recovery, proof, authority
        )

    def observe_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        return self._io.observe_preparation(request)

    def execute_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        observed = self.observe_preparation(request)
        if observed.state != "absent":
            return observed
        if request.phase == "materialize":
            materialization = self.materialize(request.plan, request.authority)
            receipt = observed.model_copy(
                update={"state": "materialized", "materialization": materialization}
            )
        else:
            materialization = self._io.preparation_materialization(request)
            recovery = self.prepare(materialization, request.binding, request.authority)
            receipt = ExternalBootPreparationObservation(
                state="prepared",
                binding=request.binding,
                plan_identity=request.plan.identity,
                authority=request.authority,
                operation_identity=request.operation_identity,
                materialization=materialization,
                recovery_point=recovery,
            )
        return self._io.publish_preparation(receipt)

    def adopt_preparation(
        self,
        request: ExternalBootPreparationRequest,
        predecessor: ExternalBootPreparationRequest,
        predecessor_receipt_identity: str,
    ) -> ExternalBootPreparationObservation:
        return self._io.adopt_preparation(request, predecessor, predecessor_receipt_identity)

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization:
        expected = ExpectedOperationOwnership(
            UUID(plan.ownership.system_id), UUID(plan.ownership.run_id), None
        )
        with self._io.open(authority, expected) as operation:
            materialization = operation.materialize(plan)
            if (
                materialization.provider_kind != "local-libvirt"
                or materialization.ownership.system_id != plan.ownership.system_id
                or materialization.ownership.run_id != plan.ownership.run_id
                or materialization.plan_identity != plan.identity
                or materialization.architecture != plan.architecture
            ):
                raise ValueError("external-boot materialization does not match plan")
            return materialization

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> RecoveryPoint:
        expected = _expected_binding(binding)
        with self._io.open(authority, expected) as operation:
            if (
                binding.system_id != materialization.ownership.system_id
                or binding.run_id != materialization.ownership.run_id
            ):
                raise ValueError("external-boot binding does not match materialization")
            metadata = operation.prepare(materialization, binding)
            if (
                metadata.binding != binding
                or metadata.materialization_identity != materialization.identity
                or metadata.plan_identity != materialization.plan_identity
            ):
                raise ValueError("external-boot prepared metadata does not match request")
            point = RecoveryPoint(
                binding=binding,
                plan_identity=metadata.plan_identity,
                materialization_identity=metadata.materialization_identity,
                recovery_ref=operation.recovery_ref(binding),
                source_state=metadata.source_state,
                target_state=metadata.target_state,
            )
            self._validate_metadata(point, metadata)
            return point

    def activate(
        self,
        recovery: RecoveryPoint,
        authority: OpaqueProviderRef,
        *,
        local_timing: LocalExternalBootTimingV1 | None = None,
    ) -> None:
        opening = (
            self._io.open(authority, _expected_binding(recovery.binding))
            if local_timing is None
            else self._io.open(
                authority, _expected_binding(recovery.binding), local_timing=local_timing
            )
        )
        with opening as operation:
            metadata = self._reopen(operation, recovery)
            resumable = {
                "pre-stop-intent",
                "move-ready",
                "old-aside",
                "rollback-ready",
                "rollback-complete",
                "new-live",
                "publication-complete",
                "module-restored",
                "target-defined",
            }
            if metadata.phase not in resumable:
                raise ValueError("external-boot activation phase is not resumable")
            if metadata.phase == "target-defined":
                return
            if metadata.phase != "module-restored":
                operation.activate_modules(metadata)
                metadata = self._reopen(operation, recovery)
            if metadata.phase == "module-restored":
                operation.define_target(metadata)

    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation:
        with self._io.open(authority, _expected_binding(recovery.binding)) as operation:
            metadata = self._reopen(operation, recovery)
            if metadata.phase != "target-defined":
                raise ValueError("external-boot target-defined evidence is required")
            return operation.observe_running(metadata)

    def recover(
        self,
        recovery: RecoveryPoint,
        authority: OpaqueProviderRef,
        *,
        local_timing: LocalExternalBootTimingV1 | None = None,
        restore_power: bool = True,
        acknowledged: LocalObservedState | None = None,
    ) -> None:
        """Resume recovery to ``recovered``, restoring prior power unless told not to.

        ``acknowledged`` is the conflict observation an administrator acknowledged
        (ADR-0583): a third definition or module tree is replaced by the recorded source only
        when it is exactly that observation, and each write re-checks its own component. Without
        it a third identity is refused.

        System teardown passes ``restore_power=False`` (#2898): it destroys the domain next,
        so a start would only boot a guest it is about to kill. ``define_source`` records
        ``source-restored`` only over the inactive source definition, which is the state a
        teardown needs, so that point is recorded ``recovered`` without a start.
        """
        opening = (
            self._io.open(authority, _expected_binding(recovery.binding))
            if local_timing is None
            else self._io.open(
                authority, _expected_binding(recovery.binding), local_timing=local_timing
            )
        )
        with opening as operation:
            metadata = self._reopen(operation, recovery)
            if metadata.phase in {"recovered", "cleaned"}:
                return
            if metadata.phase not in {
                "pre-stop-intent",
                "target-defined",
                "move-ready",
                "old-aside",
                "rollback-ready",
                "rollback-complete",
                "new-live",
                "publication-complete",
                "absence-live",
                "absence-complete",
                "absence-cleaned",
                "module-restored",
                "source-restored",
            }:
                raise ProviderRecoveryRefusal("external-boot recovery phase is not resumable")
            if metadata.phase not in {"source-restored"}:
                if acknowledged is None:
                    operation.recover_modules(metadata)
                else:
                    operation.recover_modules(metadata, acknowledged)
                metadata = self._reopen(operation, recovery)
            if metadata.phase == "module-restored":
                if acknowledged is None:
                    operation.define_source(metadata)
                else:
                    operation.define_source(metadata, acknowledged)
                metadata = self._reopen(operation, recovery)
            if metadata.phase == "source-restored":
                if restore_power:
                    operation.restore_power(metadata)
                else:
                    operation.record_phase(metadata, "recovered")

    def cleanup_is_accounted(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> bool:
        """Whether accounted cleanup evidence for this exact point already exists.

        The authority adapter asks before cleaning so observation stays read-only. A crash
        while publishing a tombstone can leave matching producer residue beside it; only the
        separately proof-gated ``finalize_tombstone`` path validates and completes that
        interrupted publication.

        Kept separate from ``cleanup`` rather than returned by it: ``cleanup`` is part of the
        provider-neutral ``ExternalBootPorts`` protocol, which the remote provider also
        implements, and that signature is not this change's to widen.
        """
        with self._io.open(authority, _expected_binding(recovery.binding)) as operation:
            return operation.cleanup_complete(recovery)

    def cleanup(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        with self._io.open(authority, _expected_binding(recovery.binding)) as operation:
            if operation.cleanup_complete(recovery):
                return
            metadata = self._reopen(operation, recovery)
            if metadata.phase == "cleaned":
                return
            if metadata.phase != "recovered":
                raise ValueError("external-boot recovery must complete before cleanup")
            operation.cleanup(metadata, recovery_point_digest(recovery))

    @staticmethod
    def _quarantine_observation(
        receipt: CleanupQuarantineReceiptV1,
    ) -> RecoveryObjectObservation:
        point = receipt.tombstone.recovery_point
        proof = receipt.proof
        binding = RecoveryObjectBinding(
            record_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"kdive/local-recovery/{point.recovery_ref.ref}/{proof.journal_digest}",
                )
            ),
            binding=point.binding,
            kind="recovery-record",
            reference=point.recovery_ref,
            ownership_digest=receipt.tombstone.point_digest,
            operation_identity=proof.operation_id,
            attempt_id=proof.attempt_id,
            mutation_journal_sequence=proof.journal_sequence,
            mutation_journal_digest=proof.journal_digest,
            reserved_bytes=0,
        )
        digest = (
            "sha256:"
            + hashlib.sha256(
                b"kdive-local-cleanup-quarantine-observation-v1\0"
                + _canonical_record_bytes(receipt)
            ).hexdigest()
        )
        return RecoveryObjectObservation(
            binding=binding,
            present=True,
            managed=receipt.managed,
            observed_digest=digest,
        )

    def quarantined_objects(
        self,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> tuple[RecoveryObjectObservation, ...]:
        with self._io.open_pinned(authority, _expected_binding(binding)):
            receipt = self._io.read_cleanup_quarantine(binding)
        if receipt is None or receipt.managed:
            return ()
        return (self._quarantine_observation(receipt),)

    def record_cleanup_quarantine(
        self,
        recovery: RecoveryPoint,
        proof: FinalizeCleanupProof,
        authority: OpaqueProviderRef,
    ) -> None:
        if proof.binding != recovery.binding or proof.point_digest != recovery_point_digest(
            recovery
        ):
            raise ValueError("external-boot cleanup proof does not match recovery point")
        # Pinned since ADR-0710's 2026-09-30 amendment: a session open creates nothing, so
        # exact recovery absence stays provable after cleanup pruned the parents (#2898).
        with self._io.open(authority, _expected_binding(recovery.binding)):
            self._io.record_cleanup_quarantine(recovery, proof)

    def observe_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef
    ) -> RecoveryObjectObservation:
        with self._io.open_pinned(authority, _expected_binding(binding.binding)):
            receipt = self._io.read_cleanup_quarantine(binding.binding)
        if receipt is None:
            return RecoveryObjectObservation(
                binding=binding,
                present=False,
                managed=False,
                observed_digest=binding.mutation_journal_digest,
            )
        observation = self._quarantine_observation(receipt)
        if observation.binding != binding:
            raise ValueError("cleanup quarantine ownership conflicts with request")
        return observation

    def delete_recovery_object(
        self,
        binding: RecoveryObjectBinding,
        authority: OpaqueProviderRef,
        expected_observed_digest: Digest,
    ) -> RecoveryObjectObservation:
        observed = self.observe_object(binding, authority)
        if observed.observed_digest != expected_observed_digest:
            raise ValueError("cleanup quarantine observation changed before delete")
        # The re-read and delete run under the pinned session (ADR-0710 amendment, 2026-10-01).
        with self._io.open_pinned(authority, _expected_binding(binding.binding)):
            receipt = self._io.read_cleanup_quarantine(binding.binding)
            if receipt is not None:
                current = self._quarantine_observation(receipt)
                if current.observed_digest != expected_observed_digest or current.managed:
                    raise ValueError("cleanup quarantine observation changed before delete")
                self._io.finalize_tombstone(receipt.tombstone.recovery_point, receipt.proof)
        return self.observe_object(binding, authority)

    def adopt_object(
        self,
        binding: RecoveryObjectBinding,
        authority: OpaqueProviderRef,
        expected_observed_digest: Digest,
    ) -> RecoveryObjectObservation:
        observed = self.observe_object(binding, authority)
        if observed.observed_digest != expected_observed_digest or not observed.present:
            raise ValueError("cleanup quarantine observation changed before adopt")
        # Pinned like delete_recovery_object (ADR-0710 amendment, 2026-10-01).
        with self._io.open_pinned(authority, _expected_binding(binding.binding)):
            receipt = self._io.read_cleanup_quarantine(binding.binding)
            if receipt is None:
                raise ValueError("cleanup quarantine disappeared before adopt")
            # The digest covers `managed`: an unmanaged expectation refuses a managed receipt,
            # while a retry holding the managed digest re-adopts it as an idempotent no-op.
            if self._quarantine_observation(receipt).observed_digest != expected_observed_digest:
                raise ValueError("cleanup quarantine observation changed before adopt")
            self._io.adopt_cleanup_quarantine(receipt)
        return self.observe_object(binding, authority)

    def recovery_point(
        self, binding: ExternalBootActivationBinding, authority: OpaqueProviderRef
    ) -> RecoveryPoint:
        """Resolve the durable recovery point an activation already owns.

        The authority seam addresses an activation by its owner identities, not by a
        recovery point it never held, so the point is rebuilt from the durable record
        under the same authenticated lease every other operation uses.

        The recovery reference encodes only System and activation, so the durable record
        is checked against the whole requested binding here. Without that, a Run mismatch
        would surface two layers down as an opaque lease failure rather than at the seam
        that made the claim.
        """
        with self._io.open(authority, _expected_binding(binding)) as operation:
            metadata = operation.reopen_binding(binding)
            if metadata.binding != binding:
                raise ValueError("external-boot recovery record does not match the binding")
            return RecoveryPoint(
                binding=metadata.binding,
                plan_identity=metadata.plan_identity,
                materialization_identity=metadata.materialization_identity,
                recovery_ref=operation.recovery_ref(binding),
                source_state=metadata.source_state,
                target_state=metadata.target_state,
            )

    def cleanup_receipt(
        self, binding: ExternalBootActivationBinding, authority: OpaqueProviderRef
    ) -> RecoveryPoint | None:
        """Reconstruct an exact accounted-cleanup receipt after authority restart."""
        with self._io.open(authority, _expected_binding(binding)) as operation:
            try:
                tombstone = operation.reopen_cleanup_tombstone(binding)
            except FileNotFoundError:
                return None
            if tombstone.binding != binding:
                raise ValueError("cleanup tombstone does not match requested binding")
            return tombstone.recovery_point

    def abort_preparation(
        self,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
        *,
        plan_identity: Digest,
        source_identity: str,
        target_identity: str,
    ) -> PartialAbortResult:
        with self._io.open(authority, _expected_binding(binding)) as operation:
            return cast(LocalPartialRecoveryOperation, operation).abort_preparation(
                binding, plan_identity, source_identity, target_identity, authority
            )

    def abort_system_teardown_preparation(
        self,
        intent: LocalSystemTeardownIntentV1,
        authority: OpaqueProviderRef,
    ) -> PartialAbortResult:
        with self._io.open(authority, _expected_binding(intent.binding)) as operation:
            return cast(LocalPartialRecoveryOperation, operation).abort_system_teardown_preparation(
                intent.binding, intent.plan_identity, authority
            )

    def recovery_is_absent(
        self, binding: ExternalBootActivationBinding, authority: OpaqueProviderRef
    ) -> bool:
        with self._io.open(authority, _expected_binding(binding)) as operation:
            return cast(LocalPartialRecoveryOperation, operation).recovery_is_absent(binding)

    def stranded_facts(
        self, binding: ExternalBootActivationBinding, domain_name: str
    ) -> StrandedAuthorityFacts:
        return cast(LocalStrandedObservationIO, self._io).stranded_facts(binding, domain_name)

    def observe_state(
        self, binding: ExternalBootActivationBinding, authority: OpaqueProviderRef
    ) -> LocalObservedState:
        """Read observed provider state for an activation without requiring a phase."""
        with self._io.open(authority, _expected_binding(binding)) as operation:
            metadata = operation.reopen_binding(binding)
            return operation.observe_state(metadata)

    def finalize_cleanup_tombstone(
        self,
        recovery: RecoveryPoint,
        proof: FinalizeCleanupProof,
        authority: OpaqueProviderRef,
    ) -> None:
        if proof.binding != recovery.binding or proof.point_digest != recovery_point_digest(
            recovery
        ):
            raise ValueError("external-boot cleanup proof does not match recovery point")
        # The authority supplies the anchored mutation-started proof as
        # ``AuthorityCommitContextV1`` (ADR-0592).  The local seam deliberately does not
        # decode it; it compares the closed owner/point fields and handles present or
        # post-delete absence idempotently. The delete runs under the pinned session, as
        # ``record_cleanup_quarantine`` does (ADR-0710 amendment, 2026-09-30).
        with self._io.open(authority, _expected_binding(recovery.binding)):
            receipt = self._io.read_cleanup_quarantine(recovery.binding)
            if receipt is not None and receipt.managed:
                # An adopted receipt is the operator's; finalize leaves it in place (ADR-0787).
                return
            self._io.finalize_tombstone(recovery, proof)

    def _reopen(
        self, operation: LocalExternalBootOperation, recovery: RecoveryPoint
    ) -> LocalRecoveryMetadataV1:
        metadata = operation.reopen(recovery)
        self._validate_metadata(recovery, metadata)
        return metadata

    @staticmethod
    def _validate_metadata(recovery: RecoveryPoint, metadata: LocalRecoveryMetadataV1) -> None:
        if (
            metadata.binding != recovery.binding
            or metadata.plan_identity != recovery.plan_identity
            or metadata.materialization_identity != recovery.materialization_identity
            or metadata.source_state != recovery.source_state
            or metadata.target_state != recovery.target_state
        ):
            raise ValueError("external-boot recovery metadata does not match recovery point")


def _expected_binding(binding: ExternalBootActivationBinding) -> ExpectedOperationOwnership:
    return ExpectedOperationOwnership(
        UUID(binding.system_id), UUID(binding.run_id), UUID(binding.activation_id)
    )


def _same_domain_definition(observed_xml: str, expected_xml: str) -> bool:
    """Whether libvirt's readback of a defined domain is the definition kdive rendered.

    libvirt stores a defined domain in its own serialization and <os> child order, so the
    target is compared by its ADR-0583 preserved and boot-projection identities, not by bytes.
    """
    observed = parse_projected_domain_xml(observed_xml)
    expected = parse_projected_domain_xml(expected_xml)
    return preserved_element_identity(observed) == preserved_element_identity(
        expected
    ) and boot_projection_element_identity(observed) == boot_projection_element_identity(expected)


def _sync_phase(io: ModulePublicationIO, phase: PublicationPhase) -> None:
    io.guest_sync()
    io.record_phase(phase)


def _move_with_reclassification(
    io: ModulePublicationIO,
    move: Callable[[], None],
    *,
    before: ModuleLayout,
    after: ModuleLayout,
    after_phase: PublicationPhase,
) -> None:
    """Retry a no-effect move or record its exact after-effect layout."""
    try:
        move()
    except OSError as exc:
        io.require_inactive()
        observed = io.observe_layout()
        if observed == before:
            move()
            return
        if observed == after:
            _sync_phase(io, after_phase)
            return
        raise ValueError("external-boot module publication conflict") from exc


def _remove_with_reclassification(
    io: ModulePublicationIO,
    *,
    before: ModuleLayout,
    after: ModuleLayout,
    after_phase: PublicationPhase,
) -> None:
    """Retry a no-effect removal or record its exact after-effect layout."""
    try:
        io.remove_old()
    except OSError as exc:
        io.require_inactive()
        observed = io.observe_layout()
        if observed == before:
            io.remove_old()
            return
        if observed == after:
            _sync_phase(io, after_phase)
            return
        raise ValueError("external-boot module removal conflict") from exc


def advance_module_publication(
    io: ModulePublicationIO,
    *,
    phase: PublicationPhase,
    layout: ModuleLayout,
    prior: ComponentState | None,
    desired: ComponentState,
) -> None:
    """Perform the sole ADR-0586 action allowed by a present-tree restart row."""
    io.require_inactive()
    if phase == PublicationPhase.OLD_ASIDE and layout == ModuleLayout(None, desired, prior):
        try:
            io.move_staging_to_live()
        except OSError as exc:
            io.require_inactive()
            observed = io.observe_layout()
            if observed == ModuleLayout(None, desired, prior):
                if prior is None:
                    io.move_staging_to_live()
                    return
                io.record_phase(PublicationPhase.ROLLBACK_READY)
                return
            if observed == ModuleLayout(desired, None, prior):
                _sync_phase(io, PublicationPhase.NEW_LIVE)
                return
            raise ValueError("external-boot module publication conflict") from exc
        return
    rows = {
        (PublicationPhase.MOVE_READY, ModuleLayout(prior, desired, None)): lambda: (
            _move_with_reclassification(
                io,
                io.move_live_to_old,
                before=ModuleLayout(prior, desired, None),
                after=ModuleLayout(None, desired, prior),
                after_phase=PublicationPhase.OLD_ASIDE,
            )
        ),
        (PublicationPhase.MOVE_READY, ModuleLayout(None, desired, prior)): lambda: _sync_phase(
            io, PublicationPhase.OLD_ASIDE
        ),
        (PublicationPhase.OLD_ASIDE, ModuleLayout(desired, None, prior)): lambda: _sync_phase(
            io, PublicationPhase.NEW_LIVE
        ),
        (PublicationPhase.ROLLBACK_READY, ModuleLayout(None, desired, prior)): lambda: (
            _move_with_reclassification(
                io,
                io.move_old_to_live,
                before=ModuleLayout(None, desired, prior),
                after=ModuleLayout(prior, desired, None),
                after_phase=PublicationPhase.ROLLBACK_COMPLETE,
            )
        ),
        (PublicationPhase.ROLLBACK_READY, ModuleLayout(prior, desired, None)): lambda: _sync_phase(
            io, PublicationPhase.ROLLBACK_COMPLETE
        ),
        (
            PublicationPhase.ROLLBACK_COMPLETE,
            ModuleLayout(prior, desired, None),
        ): lambda: io.record_phase(PublicationPhase.MOVE_READY),
        (PublicationPhase.NEW_LIVE, ModuleLayout(desired, None, prior)): lambda: (
            _remove_with_reclassification(
                io,
                before=ModuleLayout(desired, None, prior),
                after=ModuleLayout(desired, None, None),
                after_phase=PublicationPhase.PUBLICATION_COMPLETE,
            )
        ),
        (PublicationPhase.NEW_LIVE, ModuleLayout(desired, None, None)): lambda: _sync_phase(
            io, PublicationPhase.PUBLICATION_COMPLETE
        ),
        (PublicationPhase.PUBLICATION_COMPLETE, ModuleLayout(desired, None, None)): lambda: None,
    }
    try:
        action = rows[(PublicationPhase(phase), layout)]
    except (KeyError, ValueError) as exc:
        raise ValueError("external-boot module publication conflict") from exc
    action()


def advance_absence_publication(
    io: ModulePublicationIO,
    *,
    phase: PublicationPhase,
    layout: ModuleLayout,
    prior: ComponentState,
) -> None:
    """Perform the sole ADR-0586 action allowed by an absent-tree restart row."""
    io.require_inactive()
    rows = {
        (PublicationPhase.MOVE_READY, ModuleLayout(prior, None, None)): lambda: (
            _move_with_reclassification(
                io,
                io.move_live_to_old,
                before=ModuleLayout(prior, None, None),
                after=ModuleLayout(None, None, prior),
                after_phase=PublicationPhase.ABSENCE_LIVE,
            )
        ),
        (PublicationPhase.MOVE_READY, ModuleLayout(None, None, prior)): lambda: _sync_phase(
            io, PublicationPhase.ABSENCE_LIVE
        ),
        (PublicationPhase.MOVE_READY, ModuleLayout(None, None, None)): lambda: io.record_phase(
            PublicationPhase.ABSENCE_COMPLETE
        ),
        (PublicationPhase.ABSENCE_LIVE, ModuleLayout(None, None, prior)): lambda: io.record_phase(
            PublicationPhase.ABSENCE_COMPLETE
        ),
        (PublicationPhase.ABSENCE_COMPLETE, ModuleLayout(None, None, prior)): lambda: (
            _remove_with_reclassification(
                io,
                before=ModuleLayout(None, None, prior),
                after=ModuleLayout(None, None, None),
                after_phase=PublicationPhase.ABSENCE_CLEANED,
            )
        ),
        (PublicationPhase.ABSENCE_COMPLETE, ModuleLayout(None, None, None)): lambda: _sync_phase(
            io, PublicationPhase.ABSENCE_CLEANED
        ),
        (PublicationPhase.ABSENCE_CLEANED, ModuleLayout(None, None, None)): lambda: None,
    }
    try:
        action = rows[(PublicationPhase(phase), layout)]
    except (KeyError, ValueError) as exc:
        raise ValueError("external-boot module absence conflict") from exc
    action()
