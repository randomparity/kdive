"""Host mechanisms for the local external-boot session factory (ADR-0591, #2211)."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from uuid import UUID

import pytest

from kdive.providers.local_libvirt.lifecycle.boot.session import OperationOwnership
from kdive.providers.local_libvirt.lifecycle.boot.session_mechanisms import (
    LocalArtifactRoot,
    LocalOperationLane,
    LocalOperationLease,
)
from kdive.providers.ports.external_boot import ExternalBootActivationBinding

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


def _assert_no_host_path(error: BaseException, root: Path) -> None:
    assert "owner-only service-owned directory" in str(error)
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
        _assert_no_host_path(caught.value, recovery_root)
        _assert_context_suppressed(caught.value)

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

    @pytest.mark.parametrize("component", ["root", "system"])
    def test_open_refuses_a_wide_mode_component(self, recovery_root: Path, component: str) -> None:
        if component == "root":
            recovery_root.chmod(0o755)
        else:
            _private_dir(recovery_root / str(SYSTEM_ID)).chmod(0o755)

        with pytest.raises(ValueError) as caught:
            LocalArtifactRoot(recovery_root).open(OWNERSHIP)
        # Raised by `_require_private_owned_directory` itself, which already carries no path,
        # so it is not re-wrapped and its context is not suppressed.
        _assert_no_host_path(caught.value, recovery_root)

    def test_open_refuses_a_root_that_is_not_a_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "regular-file"
        root.write_bytes(b"")
        with pytest.raises(ValueError) as caught:
            LocalArtifactRoot(root).open(OWNERSHIP)
        _assert_no_host_path(caught.value, root)
        _assert_context_suppressed(caught.value)

    def test_open_refuses_a_missing_root(self, tmp_path: Path) -> None:
        root = tmp_path / "absent"
        with pytest.raises(ValueError) as caught:
            LocalArtifactRoot(root).open(OWNERSHIP)
        _assert_no_host_path(caught.value, root)
        _assert_context_suppressed(caught.value)

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
