"""Host mechanisms for the local external-boot session factory (ADR-0591, #2211)."""

from __future__ import annotations

import errno
import io
import os
import stat
import sys
import types
from pathlib import Path
from typing import get_args
from uuid import UUID

import pytest

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    FinalizeCleanupProof,
    LocalLibvirtExternalBoot,
    ModuleArchiveCapture,
    RecoveryMetadataStore,
    TargetProjectionV1,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    ExpectedOperationOwnership,
    LocalExternalBootSession,
    LocalExternalBootSessionFactory,
    OperationOwnership,
    _unconfigured_cleanup,
    _unconfigured_observation,
    _unconfigured_readiness,
)
from kdive.providers.local_libvirt.lifecycle.boot.session_mechanisms import (
    PAYLOAD_NAMES,
    LocalArtifactRoot,
    LocalOperationLane,
    LocalOperationLease,
    LocalPayloadCleanup,
    open_libguestfs_guest,
)
from kdive.providers.ports.external_boot import ExternalBootActivationBinding
from kdive.providers.shared.runtime_paths import overlay_path
from tests.providers.local_libvirt.external_boot_support import (
    _BINDING,
    _metadata,
    _point,
    _pre_stop,
)
from tests.providers.local_libvirt.lifecycle.boot.session_support import (
    Conn,
    Domain,
    Guest,
    _xml,
)

SYSTEM_ID = UUID("11111111-1111-1111-1111-111111111111")
BINDING = ExternalBootActivationBinding(
    system_id=str(SYSTEM_ID),
    run_id="22222222-2222-2222-2222-222222222222",
    activation_id="33333333-3333-3333-3333-333333333333",
)
OWNERSHIP = OperationOwnership(SYSTEM_ID, BINDING)


def _lease() -> LocalOperationLease:
    return LocalOperationLease(system_id=SYSTEM_ID, binding=BINDING)


def _private_dir(path: Path) -> Path:
    """Create a mode-0700 directory in two steps.

    `mkdir(mode=...)` masks its argument with the umask. For 0700 no realistic umask clears
    an owner bit, so the one-step form would work here — but the moment a fixture needs a
    group or other bit it silently would not, and this is the repository's stated idiom.
    """
    path.mkdir()
    path.chmod(0o700)
    return path


_ARCHIVE_DIRECTORY = f"{BINDING.system_id}.{BINDING.activation_id}"


def _archive_directory(parent: Path) -> Path:
    """Build the per-activation recovery directory holding a published archive."""
    recovery = _private_dir(parent / _ARCHIVE_DIRECTORY)
    (recovery / "modules.tar").write_bytes(b"archive")
    return recovery


def _cleanup(
    recovery_root: Path,
    artifacts: Path,
    binding: ExternalBootActivationBinding | None = None,
) -> None:
    """Run cleanup against a descriptor the caller owns, as the session does."""
    descriptor = os.open(artifacts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        LocalPayloadCleanup(recovery_root).cleanup(descriptor, binding or BINDING)
    finally:
        os.close(descriptor)


ARTIFACT_ROOT_WRAPPED = "artifact root is not an owner-only service-owned directory"
ARTIFACT_ROOT_MODE = "artifact root must be an owner-only service-owned directory"
RECOVERY_ROOT_MODE = "recovery root must be an owner-only service-owned directory"
RECOVERY_DIR_WRAPPED = "recovery directory is not an owner-only service-owned directory"
# `_open_private_directory` hard-codes this label for *every* component it opens, including
# the artifact root's own children -- a known limitation the spec records, since fixing it
# would mean editing `external_boot.py`, which this change declares unmodified.
COMPONENT_MODE = "recovery directory must be an owner-only service-owned directory"


def _assert_refusal(error: BaseException, root: Path, expected: str) -> None:
    """Assert the refusal names the fence that actually fired, and leaks no host path.

    The label is load-bearing for the assertion, not just for the operator: every message here
    ends in the same "owner-only service-owned directory" suffix, so a test matching only that
    substring passes whichever fence fired and cannot tell a correct label from a wrong one.
    """
    assert str(error).startswith(expected), str(error)
    assert str(root) not in str(error)


def _assert_context_suppressed(error: BaseException) -> None:
    """Assert the `from None`, which a message-only assertion cannot see.

    A wrapper written `raise ... from exc` produces a byte-identical message while
    re-attaching the original `OSError` — and its `.filename`, holding the host path —
    through the chained traceback that reaches a log or a CI transcript.
    """
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


class TestOperationLane:
    def test_pin_refuses_a_foreign_lease(self) -> None:
        with pytest.raises(TypeError, match="foreign operation lease"):
            LocalOperationLane().pin(object())  # ty: ignore[invalid-argument-type]

    def test_pin_refuses_a_structural_impostor(self) -> None:
        # Nominal, not structural: an object carrying the right attribute names is still
        # not a lease. `LocalExternalBootOperationLease` is a Protocol, so a structural
        # check here would accept this and the lane would pin an identity nobody issued.
        class Impostor:
            system_id = SYSTEM_ID
            binding = BINDING

        # No `ty: ignore` here, and that is the point: `Impostor` satisfies the Protocol
        # structurally, so the type checker accepts this call. Only the runtime isinstance
        # check refuses it.
        with pytest.raises(TypeError, match="foreign operation lease"):
            LocalOperationLane().pin(Impostor())

    def test_pin_refuses_a_released_lease(self) -> None:
        lease = _lease()
        lease.release()
        with pytest.raises(RuntimeError, match="operation lease is released"):
            LocalOperationLane().pin(lease)

    def test_release_is_refused_while_a_pin_is_outstanding(self) -> None:
        lease = _lease()
        pinned = LocalOperationLane().pin(lease)
        with pytest.raises(RuntimeError, match="operation lease is pinned"):
            lease.release()
        pinned._pin.close()
        lease.release()
        assert lease.released is True

    def test_pin_returns_the_exact_lease_identity(self) -> None:
        lease = _lease()
        pinned = LocalOperationLane().pin(lease)
        assert pinned.ownership == OperationOwnership(SYSTEM_ID, BINDING)
        # The binding is carried through, not rebuilt: a reconstructed equal value would
        # satisfy the equality above while losing the identity the lease actually issued.
        assert pinned.ownership.binding is lease.binding

    def test_lane_cannot_mint_its_own_lease(self) -> None:
        # ADR-0587 assigns lease issuance to the serialization-lane context (#2212). A lane
        # that could issue one would be the synthetic identity the rejected #2126 attempt
        # reached for, so the absence of an issuing method is part of the contract.
        assert not hasattr(LocalOperationLane, "issue")

    def test_a_second_pin_keeps_the_lease_held(self) -> None:
        # Closing one pin must not release a lease another pin still holds.
        lane, lease = LocalOperationLane(), _lease()
        first, second = lane.pin(lease), lane.pin(lease)
        first._pin.close()
        with pytest.raises(RuntimeError, match="operation lease is pinned"):
            lease.release()
        second._pin.close()
        lease.release()
        assert lease.released is True


@pytest.fixture
def recovery_root(tmp_path: Path) -> Path:
    return _private_dir(tmp_path / "recovery")


class TestArtifactRoot:
    def test_open_creates_and_returns_the_system_run_directory(self, recovery_root: Path) -> None:
        # #2210 provisions the recovery root and nothing below it, so an open-only walk
        # could never succeed for a System that has not run before.
        descriptor = LocalArtifactRoot(recovery_root).open(OWNERSHIP)
        try:
            system = recovery_root / str(SYSTEM_ID)
            run = system / BINDING.run_id
            opened, on_disk = os.fstat(descriptor), run.stat()
            assert (opened.st_dev, opened.st_ino) == (on_disk.st_dev, on_disk.st_ino)
            assert stat.S_IMODE(system.stat().st_mode) == 0o700
            assert stat.S_IMODE(on_disk.st_mode) == 0o700
        finally:
            os.close(descriptor)

    @pytest.mark.parametrize("component", ["system", "run"])
    def test_open_refuses_a_symlinked_component(
        self, recovery_root: Path, tmp_path: Path, component: str
    ) -> None:
        elsewhere = _private_dir(tmp_path / "elsewhere")
        if component == "system":
            parent, name = recovery_root, str(SYSTEM_ID)
        else:
            parent = _private_dir(recovery_root / str(SYSTEM_ID))
            name = BINDING.run_id
        os.symlink(elsewhere, parent / name)

        with pytest.raises(ValueError) as caught:
            LocalArtifactRoot(recovery_root).open(OWNERSHIP)
        _assert_refusal(caught.value, recovery_root, ARTIFACT_ROOT_WRAPPED)
        _assert_context_suppressed(caught.value)
        # The symbolic errno rides along; the host path does not.
        assert str(caught.value).endswith("(ENOTDIR)")

        # `O_NOFOLLOW` is what refuses the symlink, asserted against the syscall because the
        # mechanism's contract deliberately replaces the errno with a fixed `ValueError`.
        # With `O_DIRECTORY` also set Linux reports ENOTDIR, not ELOOP — and since a regular
        # file raises ENOTDIR too, the errno alone would not prove the symlink was refused
        # *for being a symlink*. The open without the flag succeeding is what proves that.
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(NotADirectoryError) as refused:
                os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            assert refused.value.errno == errno.ENOTDIR
            followed = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
            os.close(followed)
        finally:
            os.close(parent_fd)

    @pytest.mark.parametrize(
        ("component", "expected"),
        [("root", ARTIFACT_ROOT_MODE), ("system", COMPONENT_MODE)],
    )
    def test_open_refuses_a_wide_mode_component(
        self, recovery_root: Path, component: str, expected: str
    ) -> None:
        if component == "root":
            recovery_root.chmod(0o755)
        else:
            _private_dir(recovery_root / str(SYSTEM_ID)).chmod(0o755)

        with pytest.raises(ValueError) as caught:
            LocalArtifactRoot(recovery_root).open(OWNERSHIP)
        # Raised by `_require_private_owned_directory` itself, which already carries no path,
        # so it is not re-wrapped and its context is not suppressed. The two parameters expect
        # *different* labels: only the root's own check says "artifact root", because the
        # component walk goes through `_open_private_directory`'s fixed label.
        _assert_refusal(caught.value, recovery_root, expected)

    def test_open_refuses_a_root_that_is_not_a_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "regular-file"
        root.write_bytes(b"")
        with pytest.raises(ValueError) as caught:
            LocalArtifactRoot(root).open(OWNERSHIP)
        _assert_refusal(caught.value, root, ARTIFACT_ROOT_WRAPPED)
        _assert_context_suppressed(caught.value)
        # ENOTDIR, not a generic ownership complaint: a full filesystem must not read as one.
        assert str(caught.value).endswith("(ENOTDIR)")

    def test_open_refuses_a_missing_root(self, tmp_path: Path) -> None:
        root = tmp_path / "absent"
        with pytest.raises(ValueError) as caught:
            LocalArtifactRoot(root).open(OWNERSHIP)
        _assert_refusal(caught.value, root, ARTIFACT_ROOT_WRAPPED)
        _assert_context_suppressed(caught.value)
        assert str(caught.value).endswith("(ENOENT)")

    def test_open_leaks_no_descriptor_on_failure(self, recovery_root: Path, tmp_path: Path) -> None:
        # Fail at the *second* component, so the root and system descriptors are both open
        # when the walk aborts; a failure at the first would leave nothing to leak.
        parent = _private_dir(recovery_root / str(SYSTEM_ID))
        os.symlink(_private_dir(tmp_path / "elsewhere"), parent / BINDING.run_id)
        root = LocalArtifactRoot(recovery_root)

        before = len(os.listdir("/proc/self/fd"))
        with pytest.raises(ValueError):
            root.open(OWNERSHIP)
        assert len(os.listdir("/proc/self/fd")) == before

    @pytest.mark.parametrize(
        "run_id",
        [
            # `../escape` resolves to `<recovery_root>/escape` -- out of the run component but
            # still inside the configured root. `../../escape` is the one that leaves it, and
            # is why the assertion below names `recovery_root.parent`: against the shallower
            # traversal alone that assertion could never fire.
            "../escape",
            "../../escape",
            "22222222222222222222222222222222",
            "",
        ],
    )
    def test_open_refuses_a_non_canonical_component(self, recovery_root: Path, run_id: str) -> None:
        # `model_construct` bypasses validation, which is the only way to build the impostor:
        # `ExternalBootActivationBinding` is a pydantic `_ClosedValue`, not a dataclass, so
        # `dataclasses.replace` raises `TypeError` here. `OperationOwnership` *is* a frozen
        # dataclass, which is where that confusion comes from.
        impostor = ExternalBootActivationBinding.model_construct(
            system_id=str(SYSTEM_ID),
            run_id=run_id,
            activation_id=str(BINDING.activation_id),
        )
        with pytest.raises(ValueError, match="canonical identifier"):
            LocalArtifactRoot(recovery_root).open(OperationOwnership(SYSTEM_ID, impostor))
        # The guard runs before any `mkdir`, so nothing reached the filesystem at all --
        # neither inside the configured root nor beside it.
        assert os.listdir(recovery_root) == []
        assert not (recovery_root.parent / "escape").exists()


class TestPayloadCleanup:
    def test_cleanup_removes_only_the_payload_names_under_the_descriptor(
        self, recovery_root: Path, tmp_path: Path
    ) -> None:
        artifacts = _private_dir(tmp_path / "artifacts")
        for name in (*PAYLOAD_NAMES, "keep-me"):
            (artifacts / name).write_bytes(b"payload")

        _cleanup(recovery_root, artifacts)

        assert sorted(os.listdir(artifacts)) == ["keep-me"]

    def test_cleanup_is_idempotent(self, recovery_root: Path, tmp_path: Path) -> None:
        artifacts = _private_dir(tmp_path / "artifacts")
        for name in (*PAYLOAD_NAMES, "keep-me"):
            (artifacts / name).write_bytes(b"payload")

        _cleanup(recovery_root, artifacts)
        after_first = sorted(os.listdir(artifacts))
        _cleanup(recovery_root, artifacts)

        assert sorted(os.listdir(artifacts)) == after_first

    def test_cleanup_leaves_foreign_files_in_the_recovery_directory(
        self, recovery_root: Path, tmp_path: Path
    ) -> None:
        artifacts = _private_dir(tmp_path / "artifacts")
        recovery = _archive_directory(recovery_root)
        (recovery / "foreign.json").write_bytes(b"{}")

        _cleanup(recovery_root, artifacts)

        assert not (recovery / "modules.tar").exists()
        assert sorted(os.listdir(recovery)) == ["foreign.json"]

    def test_cleanup_refuses_a_wide_mode_recovery_directory(
        self, recovery_root: Path, tmp_path: Path
    ) -> None:
        artifacts = _private_dir(tmp_path / "artifacts")
        for name in PAYLOAD_NAMES:
            (artifacts / name).write_bytes(b"payload")
        recovery = _archive_directory(recovery_root)
        recovery.chmod(0o755)

        with pytest.raises(ValueError) as caught:
            _cleanup(recovery_root, artifacts)

        _assert_refusal(caught.value, recovery_root, COMPONENT_MODE)
        # Nothing was deleted. Refusing *after* unlinking the payloads would strand the
        # activation: publish_tombstone is never reached, so every retry re-raises with the
        # payloads it would have needed already gone.
        assert sorted(os.listdir(artifacts)) == sorted(PAYLOAD_NAMES)
        assert (recovery / "modules.tar").exists()

    def test_cleanup_refuses_a_wide_mode_recovery_root(
        self, recovery_root: Path, tmp_path: Path
    ) -> None:
        # The root's *own* re-validation, on the deleting path. Distinct from the
        # per-component check above: this one fires before any component is resolved, and it
        # is what stops cleanup trusting that the root is still what startup validated.
        artifacts = _private_dir(tmp_path / "artifacts")
        for name in PAYLOAD_NAMES:
            (artifacts / name).write_bytes(b"payload")
        recovery = _archive_directory(recovery_root)
        recovery_root.chmod(0o755)

        with pytest.raises(ValueError) as caught:
            _cleanup(recovery_root, artifacts)

        _assert_refusal(caught.value, recovery_root, RECOVERY_ROOT_MODE)
        assert sorted(os.listdir(artifacts)) == sorted(PAYLOAD_NAMES)
        assert (recovery / "modules.tar").exists()

    def test_cleanup_refuses_a_symlinked_recovery_directory(
        self, recovery_root: Path, tmp_path: Path
    ) -> None:
        artifacts = _private_dir(tmp_path / "artifacts")
        for name in PAYLOAD_NAMES:
            (artifacts / name).write_bytes(b"payload")
        elsewhere = _archive_directory(tmp_path)
        os.symlink(elsewhere, recovery_root / _ARCHIVE_DIRECTORY)

        with pytest.raises(ValueError) as caught:
            _cleanup(recovery_root, artifacts)

        _assert_refusal(caught.value, recovery_root, RECOVERY_DIR_WRAPPED)
        _assert_context_suppressed(caught.value)
        assert str(caught.value).endswith("(ENOTDIR)")
        # Nothing deleted: had the payloads gone first, an attacker-planted symlink would
        # destroy them while leaving untouched the archive `finalize_tombstone` blocks on.
        assert sorted(os.listdir(artifacts)) == sorted(PAYLOAD_NAMES)
        assert (elsewhere / "modules.tar").exists()

    def test_cleanup_refuses_a_non_canonical_binding(
        self, recovery_root: Path, tmp_path: Path
    ) -> None:
        artifacts = _private_dir(tmp_path / "artifacts")
        for name in PAYLOAD_NAMES:
            (artifacts / name).write_bytes(b"payload")
        impostor = ExternalBootActivationBinding.model_construct(
            system_id=BINDING.system_id,
            run_id=BINDING.run_id,
            activation_id="../escape",
        )

        with pytest.raises(ValueError, match="canonical identifier"):
            _cleanup(recovery_root, artifacts, impostor)

        # Refused before anything was deleted, unlike every other refusal here.
        assert sorted(os.listdir(artifacts)) == sorted(PAYLOAD_NAMES)

    def test_payload_names_match_the_target_projection_filenames(self) -> None:
        # Discover the fields rather than listing them. A hard-coded list of the three known
        # names catches a *rename* and misses an *added* fourth artifact entirely: a new
        # `dtb_filename: Literal["dtb"]` would never enter `expected`, the equality would
        # still hold, and cleanup would silently stop removing it.
        expected = set()
        for name, field in TargetProjectionV1.model_fields.items():
            if not name.endswith("_filename"):
                continue
            args = get_args(field.annotation)
            # Literal["x"] -> ("x",);  Literal["x"] | None -> (Literal["x"], NoneType)
            expected.add(args[0] if isinstance(args[0], str) else get_args(args[0])[0])

        # Not decoration: an unwrapping that silently yielded nothing would make the equality
        # below vacuous, which is the exact failure this test exists to prevent.
        assert expected, "no _filename fields discovered -- the unwrapping is wrong"
        assert set(PAYLOAD_NAMES) == expected


def _session(
    artifacts: Path,
    *,
    active: bool = False,
    binding: ExternalBootActivationBinding = _BINDING,
    **mechanisms: object,
) -> LocalExternalBootSession:
    """Open a real session bound to `_BINDING`, with a real artifact descriptor.

    Deliberately not `test_session.py`'s `_factory`, which hard-codes `test_session.BINDING`
    and hands out the fake descriptor 41. `_ConcreteSession.cleanup_payloads` passes
    `self._binding` — the one the pinned lease carried — so a session built on the other
    binding would aim the archive removal at a directory that never existed, hit the
    idempotence rule, and report success. That is the trap that would make this proof vacuous.
    """
    system_id = UUID(binding.system_id)
    events: list[str] = []
    domain = Domain(events, _xml(overlay=overlay_path(system_id), system_id=system_id))
    domain.active = active
    factory = LocalExternalBootSessionFactory(
        connect=lambda: Conn(events, domain),
        pin_lease=LocalOperationLane().pin,
        open_artifact_root=lambda _ownership: os.open(
            artifacts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        ),
        open_guest=lambda: Guest(events),
        worker_pid=4242,
        # The production builder leaves `open_overlay` on its real default, which would open
        # `/var/lib/kdive/rootfs/...`. Overridden here so these tests exercise the session,
        # not the host filesystem layout.
        open_overlay=lambda _path: os.open(os.devnull, os.O_RDONLY),
        fstat_overlay=lambda _fd: (8, 9, stat.S_IFREG | 0o600),
        close_overlay_descriptor=os.close,
        **mechanisms,  # ty: ignore[invalid-argument-type]
    )
    lease = LocalOperationLease(system_id=system_id, binding=binding)
    expected = ExpectedOperationOwnership(
        system_id, UUID(binding.run_id), UUID(binding.activation_id)
    )
    return factory.open(lease, expected)


def _recovery_directory(recovery_root: Path) -> Path:
    return recovery_root / f"{_BINDING.system_id}.{_BINDING.activation_id}"


def _archived_activation(store: RecoveryMetadataStore, archive: bytes = b"module archive"):
    """Drive a real store to a `recovered` activation whose `modules.tar` is really published.

    A stubbed store is the vacuous form this proof exists to avoid, so every step here is the
    production one. `_metadata()` defaults `capture={"state": "absent"}` and
    `prior_power="running"`; both are overridden, which is why no existing test reaches this
    path.
    """
    template = _metadata().model_copy(update={"prior_power": "inactive"})
    intent = _pre_stop(template)
    reference = store.publish_pre_stop(intent)
    sink = store.recovery_archive_sink(reference, intent)
    try:
        archive_sha256, archive_bytes = sink.publish(io.BytesIO(archive))
    finally:
        sink.close()
    capture = ModuleArchiveCapture(
        manifest="sha256:" + "3" * 64,
        entry_count=0,
        uncompressed_bytes=0,
        archive_sha256=archive_sha256,
        archive_bytes=archive_bytes,
    )
    completed = store.complete_preparation(
        reference, intent, template.model_copy(update={"capture": capture})
    )
    recovered = store.record_phase(reference, _BINDING, completed, "recovered")
    return reference, recovered


def _finalize(store: RecoveryMetadataStore, reference, recovered) -> None:
    point = _point(recovered)
    digest = LocalLibvirtExternalBoot.point_digest(point)
    store.publish_tombstone(reference, _BINDING, recovered, digest)
    proof = FinalizeCleanupProof(
        point_digest=digest,
        binding=_BINDING,
        operation_id="00000000-0000-0000-0000-000000000004",
        attempt_id="00000000-0000-0000-0000-000000000005",
        journal_sequence=7,
        journal_digest="sha256:" + "4" * 64,
    )
    store.finalize_tombstone(reference, point, proof)


class TestCleanupReachability:
    def test_finalize_tombstone_succeeds_after_cleanup_of_an_archived_activation(
        self, tmp_path: Path
    ) -> None:
        recovery_root = _private_dir(tmp_path / "recovery")
        artifacts = _private_dir(tmp_path / "artifacts")
        for name in PAYLOAD_NAMES:
            (artifacts / name).write_bytes(b"payload")

        with RecoveryMetadataStore(recovery_root) as store:
            reference, recovered = _archived_activation(store)
            assert (_recovery_directory(recovery_root) / "modules.tar").exists()

            # Drive the session, not the mechanism. Calling `LocalPayloadCleanup.cleanup`
            # directly bypasses `require_inactive()`, so this proof would go green whether or
            # not that gate blocks the real path.
            session = _session(
                artifacts, cleanup_payloads=LocalPayloadCleanup(recovery_root).cleanup
            )
            session.cleanup_payloads()
            session.close()

            _finalize(store, reference, recovered)

        assert not _recovery_directory(recovery_root).exists()
        assert os.listdir(artifacts) == []

    def test_cleanup_is_blocked_while_the_domain_is_active(self, tmp_path: Path) -> None:
        recovery_root = _private_dir(tmp_path / "recovery")
        artifacts = _private_dir(tmp_path / "artifacts")
        for name in PAYLOAD_NAMES:
            (artifacts / name).write_bytes(b"payload")

        with RecoveryMetadataStore(recovery_root) as store:
            _archived_activation(store)
            session = _session(
                artifacts,
                active=True,
                cleanup_payloads=LocalPayloadCleanup(recovery_root).cleanup,
            )
            with pytest.raises(RuntimeError, match="domain must be inactive"):
                session.cleanup_payloads()
            session.close()

        assert sorted(os.listdir(artifacts)) == sorted(PAYLOAD_NAMES)
        assert (_recovery_directory(recovery_root) / "modules.tar").exists()
        assert not (_recovery_directory(recovery_root) / "tombstone.json").exists()

    # What the test above proves and does not prove. It proves the gate fires on an active
    # domain. It does NOT exercise `restore_power`, so it does not demonstrate the link from
    # `prior_power == "running"` to an active domain at cleanup time — that link is
    # established by reading `restore_power`, whose "running" arm reaches
    # `record_phase(..., "recovered")` only from the branch requiring `active`. Closing that
    # gap needs an integration-level test over the whole recover-then-cleanup path, which is
    # recorded as a residual rather than written here.


class _RecordingGuestFS:
    """A libguestfs stand-in that records every call the opener might wrongly make."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        def record(*_args: object, **_kwargs: object) -> None:
            self.calls.append(name)

        return record


class TestOpenGuest:
    def test_open_guest_attaches_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handles: list[_RecordingGuestFS] = []

        def build(**kwargs: object) -> _RecordingGuestFS:
            handles.append(_RecordingGuestFS(**kwargs))
            return handles[-1]

        module = types.ModuleType("guestfs")
        module.GuestFS = build  # ty: ignore[unresolved-attribute]
        monkeypatch.setitem(sys.modules, "guestfs", module)

        guest = open_libguestfs_guest()

        assert guest is handles[0]
        assert handles[0].kwargs == {"python_return_dict": True}
        # `_ConcreteSession._open_guest_context` owns the drive, launch and mount, and only
        # after `require_inactive()`. An opener that did any of it here would move guest
        # access outside that gate.
        assert handles[0].calls == []

    def test_session_refuses_guest_access_while_the_domain_is_active(self, tmp_path: Path) -> None:
        artifacts = _private_dir(tmp_path / "artifacts")
        session = _session(artifacts, active=True)
        try:
            # The refusal comes from the existing `require_inactive` path, not from a new
            # check in the opener -- the opener is reached only inside the guest context.
            with pytest.raises(RuntimeError, match="domain must be inactive"), session.guest():
                pass
        finally:
            session.close()


def _noop_readiness(_system_id: UUID) -> object:
    raise AssertionError("readiness must not be reached in these tests")


def _noop_observation(_system_id: UUID) -> object:
    raise AssertionError("observation must not be reached in these tests")


def _noop_cleanup(_root_fd: int, _binding: ExternalBootActivationBinding) -> None:
    raise AssertionError("cleanup must not be reached in these tests")


class TestFailClosedDefaults:
    """Each default is proven reachable independently, by omitting exactly that one.

    Deliberately not `test_session.py`'s `_factory` helper: it accepts only `events`, `domain`
    and `pin_lease`, so it omits `readiness`, `observe_running` and `cleanup_payloads`
    *together* and cannot express "omit exactly one". The three bound mechanisms below raise
    `AssertionError` if reached, so a test that passes because the wrong default fired fails
    instead of quietly agreeing.
    """

    def test_unconfigured_readiness_raises(self, tmp_path: Path) -> None:
        session = _session(
            _private_dir(tmp_path / "artifacts"),
            observe_running=_noop_observation,
            cleanup_payloads=_noop_cleanup,
        )
        try:
            with pytest.raises(
                RuntimeError, match="local external-boot readiness is not configured"
            ):
                session.readiness()
        finally:
            session.close()

    def test_unconfigured_observation_raises(self, tmp_path: Path) -> None:
        session = _session(
            _private_dir(tmp_path / "artifacts"),
            readiness=_noop_readiness,
            cleanup_payloads=_noop_cleanup,
        )
        try:
            with pytest.raises(
                RuntimeError, match="local external-boot running observation is not configured"
            ):
                session.observe_running()
        finally:
            session.close()

    def test_unconfigured_cleanup_raises(self, tmp_path: Path) -> None:
        session = _session(
            _private_dir(tmp_path / "artifacts"),
            readiness=_noop_readiness,
            observe_running=_noop_observation,
        )
        try:
            with pytest.raises(
                RuntimeError, match="local external-boot payload cleanup is not configured"
            ):
                session.cleanup_payloads()
        finally:
            session.close()

    def test_factory_defaults_are_the_unconfigured_functions(self) -> None:
        # Identity, not just "raises RuntimeError": a permissive replacement that happened to
        # raise something would satisfy a message assertion while removing the guard.
        factory = LocalExternalBootSessionFactory(
            pin_lease=LocalOperationLane().pin,
            connect=lambda: Conn([], Domain([])),
            open_artifact_root=lambda _ownership: 41,
            open_guest=lambda: Guest([]),
        )

        assert factory._readiness is _unconfigured_readiness
        assert factory._observe_running is _unconfigured_observation
        assert factory._cleanup_payloads is _unconfigured_cleanup
