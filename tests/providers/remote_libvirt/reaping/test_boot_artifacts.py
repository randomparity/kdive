"""Remote external-boot volume ownership and orphan reaping (ADR-0583/#2119)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Literal, cast
from uuid import UUID

from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_volumes import (
    artifact_partial_volume_name,
    artifact_volume_name,
    render_boot_artifact_volume_xml,
)
from kdive.providers.remote_libvirt.reaping.boot_artifacts import (
    BootArtifactReaperConn,
    BootArtifactVolume,
    list_owned_boot_artifacts,
    reap_orphaned_boot_artifacts,
)

SYSTEM = UUID("00000000-0000-0000-0000-000000000003")
RUN = UUID("00000000-0000-0000-0000-000000000002")
ATTEMPT = UUID("00000000-0000-0000-0000-000000000004")


class _Volume:
    def __init__(self, name: str, xml: str, data: bytes = b"kernel") -> None:
        self._name = name
        self._xml = xml
        self.data = data
        self.deleted = False

    def name(self) -> str:
        return self._name

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        del flags
        return self._xml

    def delete(self, flags: int = 0) -> int:
        del flags
        self.deleted = True
        return 0

    def download(self, stream: object, offset: int, length: int, flags: int = 0) -> int:
        del offset, length, flags
        assert isinstance(stream, _Stream)
        stream.data = self.data
        return 0


class _Stream:
    def __init__(self) -> None:
        self.data = b""

    def recvAll(self, callback: Callable[[object, bytes, object], None], opaque: object) -> None:  # noqa: N802
        callback(self, self.data, opaque)

    def finish(self) -> int:
        return 0

    def abort(self) -> int:
        return 0


class _Pool:
    def __init__(self, volumes: list[_Volume]) -> None:
        self.volumes = volumes

    def listAllVolumes(self, flags: int = 0) -> list[_Volume]:  # noqa: N802
        del flags
        return self.volumes

    def refresh(self, flags: int = 0) -> int:
        del flags
        return 0


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        assert name == "boot-artifacts"
        return self.pool

    def newStream(self, flags: int = 0) -> _Stream:  # noqa: N802
        del flags
        return _Stream()


def _xml(kind: str, digest: str, *, attempt: UUID | None = None) -> str:
    return render_boot_artifact_volume_xml(
        "ignored",
        capacity_bytes=5,
        kind=cast("Literal['kernel', 'initrd']", kind),
        system_id=SYSTEM,
        run_id=RUN,
        payload_digest=digest,
        attempt_id=attempt,
    )


def test_listing_accepts_only_metadata_matching_the_deterministic_name() -> None:
    digest = "sha256:" + hashlib.sha256(b"kernel").hexdigest()
    final = _Volume(artifact_volume_name("kernel", SYSTEM, RUN), _xml("kernel", digest))
    foreign = _Volume("kdive-kernel-foreign", _xml("kernel", digest))
    malformed = _Volume(artifact_volume_name("kernel", SYSTEM, RUN), "<volume/>")

    result = list_owned_boot_artifacts(
        cast("BootArtifactReaperConn", _Conn(_Pool([final, foreign, malformed]))), "boot-artifacts"
    )

    assert result == [
        BootArtifactVolume(
            name=final.name(),
            kind="kernel",
            system_id=SYSTEM,
            run_id=RUN,
            digest=digest,
            partial=False,
            attempt_id=None,
        )
    ]


def test_reap_removes_orphaned_owned_final_and_partial_but_preserves_mismatch() -> None:
    digest = "sha256:" + hashlib.sha256(b"kernel").hexdigest()
    orphan_final = _Volume(artifact_volume_name("kernel", SYSTEM, RUN), _xml("kernel", digest))
    orphan_partial = _Volume(
        artifact_partial_volume_name("kernel", SYSTEM, RUN, b"kernel", ATTEMPT),
        _xml("kernel", digest, attempt=ATTEMPT),
    )
    mismatch = _Volume(artifact_volume_name("kernel", SYSTEM, RUN), _xml("initrd", digest))
    pool = _Pool([orphan_final, orphan_partial, mismatch])

    removed = reap_orphaned_boot_artifacts(
        cast("BootArtifactReaperConn", _Conn(pool)), "boot-artifacts", live_owners=set()
    )

    assert removed == 2
    assert orphan_final.deleted and orphan_partial.deleted
    assert not mismatch.deleted


def test_reap_keeps_a_live_owner_and_a_foreign_name() -> None:
    digest = "sha256:" + hashlib.sha256(b"kernel").hexdigest()
    volume = _Volume(artifact_volume_name("kernel", SYSTEM, RUN), _xml("kernel", digest))
    pool = _Pool([volume])

    assert (
        reap_orphaned_boot_artifacts(
            cast("BootArtifactReaperConn", _Conn(pool)),
            "boot-artifacts",
            live_owners={("kernel", SYSTEM, RUN, digest)},
        )
        == 0
    )
    assert not volume.deleted
