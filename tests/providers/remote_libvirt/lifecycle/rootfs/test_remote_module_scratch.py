"""The appliance's scratch volume is formatted exactly once per attempt (ADR-0585)."""

import subprocess
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_scratch import (
    HEAD_BYTES,
    ScratchState,
    classify_head,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    SCRATCH_CAPACITY_BYTES,
    prepare_attempt_volumes,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes_support import (
    SCRATCH_NAME,
    Conn,
    Volume,
    _detached_all,
    request,
)

_MAGIC_OFFSET = 1080


def _scratch(conn: Conn) -> Volume:
    return conn.pool.volumes[SCRATCH_NAME]


def _assemble(volume: Volume, path: Path) -> Path:
    """Lay the uploaded ranges into a sparse file, as the volume would hold them."""
    with path.open("wb") as handle:
        handle.truncate(SCRATCH_CAPACITY_BYTES)
        for offset, blob in volume.ranges.items():
            handle.seek(offset)
            handle.write(blob)
    return path


def _debugfs(image: Path, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["debugfs", "-R", command, str(image)], capture_output=True, text=True, check=False
    )


def test_created_scratch_is_a_mountable_empty_ext4(tmp_path: Path) -> None:
    conn = Conn()

    prepare_attempt_volumes(conn, request(tmp_path))

    image = _assemble(_scratch(conn), tmp_path / "scratch.img")
    listing = _debugfs(image, "ls -l /")
    assert listing.returncode == 0
    assert "lost+found" in listing.stdout
    missing = _debugfs(image, "cat /result-v1.json")
    assert "File not found" in missing.stderr


def test_superblock_block_is_uploaded_last_and_only_metadata_is_sent(tmp_path: Path) -> None:
    conn = Conn()

    prepare_attempt_volumes(conn, request(tmp_path))

    log = _scratch(conn).upload_log
    assert log[-1] == (0, HEAD_BYTES)
    assert all(offset >= HEAD_BYTES for offset, _length in log[:-1])
    assert sum(length for _offset, length in log) < 256 * 1024**2


def test_retry_never_touches_a_formatted_scratch(tmp_path: Path) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    prepare_attempt_volumes(conn, wanted)
    scratch = _scratch(conn)
    scratch.ranges[HEAD_BYTES * 4] = b"appliance phase record"
    uploads = list(scratch.upload_log)
    before = dict(scratch.ranges)

    prepare_attempt_volumes(conn, wanted)

    assert scratch.upload_log == uploads
    assert scratch.ranges == before


def test_blank_scratch_is_formatted_on_retry_after_the_detachment_proof(tmp_path: Path) -> None:
    conn = Conn()
    wanted = request(tmp_path, inspect_attachments=_detached_all)
    prepare_attempt_volumes(conn, wanted)
    scratch = _scratch(conn)
    scratch.ranges.clear()
    scratch.upload_log.clear()

    prepare_attempt_volumes(conn, wanted)

    assert scratch.upload_log[-1] == (0, HEAD_BYTES)
    assert _debugfs(_assemble(scratch, tmp_path / "again.img"), "ls /").returncode == 0


def test_half_formatted_scratch_without_a_superblock_block_is_finished(tmp_path: Path) -> None:
    conn = Conn()
    wanted = request(tmp_path, inspect_attachments=_detached_all)
    prepare_attempt_volumes(conn, wanted)
    scratch = _scratch(conn)
    del scratch.ranges[0]
    scratch.upload_log.clear()

    prepare_attempt_volumes(conn, wanted)

    assert scratch.ranges[0][_MAGIC_OFFSET : _MAGIC_OFFSET + 2] == b"\x53\xef"


def test_blank_scratch_that_is_not_proven_detached_is_a_conflict(tmp_path: Path) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    prepare_attempt_volumes(conn, wanted)
    scratch = _scratch(conn)
    scratch.ranges.clear()
    scratch.upload_log.clear()

    with pytest.raises(CategorizedError, match="not detached") as raised:
        prepare_attempt_volumes(conn, wanted)

    assert raised.value.category is ErrorCategory.CONFLICT
    assert scratch.upload_log == []


def test_scratch_with_unrecognised_content_is_a_conflict_and_is_left_alone(
    tmp_path: Path,
) -> None:
    conn = Conn()
    wanted = request(tmp_path, inspect_attachments=_detached_all)
    prepare_attempt_volumes(conn, wanted)
    scratch = _scratch(conn)
    scratch.ranges.clear()
    scratch.ranges[0] = b"\x01" * HEAD_BYTES
    scratch.upload_log.clear()

    with pytest.raises(CategorizedError, match="unrecognised") as raised:
        prepare_attempt_volumes(conn, wanted)

    assert raised.value.category is ErrorCategory.CONFLICT
    assert scratch.upload_log == []


def test_mkfs_failure_removes_the_scratch_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = Conn()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "mkfs.ext4")

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(CategorizedError, match="scratch filesystem") as raised:
        prepare_attempt_volumes(conn, request(tmp_path, inspect_attachments=_detached_all))

    assert raised.value.category is ErrorCategory.BUILD_FAILURE
    assert _scratch(conn).deleted


@pytest.mark.parametrize(
    ("head", "state"),
    [
        (bytes(HEAD_BYTES), ScratchState.BLANK),
        (
            b"\x00" * _MAGIC_OFFSET + b"\x53\xef" + b"\x00" * (HEAD_BYTES - _MAGIC_OFFSET - 2),
            ScratchState.FORMATTED,
        ),
        (b"\x01" + bytes(HEAD_BYTES - 1), ScratchState.FOREIGN),
        (bytes(HEAD_BYTES - 1), ScratchState.FOREIGN),
        (b"", ScratchState.FOREIGN),
    ],
)
def test_head_classification(head: bytes, state: ScratchState) -> None:
    assert classify_head(head) is state
