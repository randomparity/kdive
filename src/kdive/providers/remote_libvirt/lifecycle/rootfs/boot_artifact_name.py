"""Durable remote boot-artifact volume-name grammar (ADR-0599)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

type BootArtifactKind = Literal["kernel", "initrd"]

BOOT_ARTIFACT_NAME_MAX_BYTES = 255
_PREFIX = "kdive-boot-v1-"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_DIGEST_HEX = r"[0-9a-f]{64}"
_NAME = re.compile(
    rf"{re.escape(_PREFIX)}(?P<kind>kernel|initrd)-(?P<system_id>{_UUID})-"
    rf"(?P<run_id>{_UUID})-(?P<digest>{_DIGEST_HEX})-"
    rf"(?:(?P<final>final)|partial-(?P<attempt_id>{_UUID}))"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class BootArtifactName:
    """Complete owner and content identity recovered from one canonical name."""

    name: str
    kind: BootArtifactKind
    system_id: UUID
    run_id: UUID
    digest: str
    partial: bool
    attempt_id: UUID | None

    @property
    def owner(self) -> tuple[BootArtifactKind, UUID, UUID, str]:
        return self.kind, self.system_id, self.run_id, self.digest


def render_boot_artifact_name(
    kind: BootArtifactKind,
    system_id: UUID,
    run_id: UUID,
    digest: str,
    *,
    attempt_id: UUID | None = None,
) -> str:
    """Render one canonical final or attempt-owned partial volume name."""
    if kind not in {"kernel", "initrd"}:
        raise ValueError("boot-artifact kind must be kernel or initrd")
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError("boot-artifact digest must be canonical sha256")
    suffix = "final" if attempt_id is None else f"partial-{attempt_id}"
    name = f"{_PREFIX}{kind}-{system_id}-{run_id}-{digest.removeprefix('sha256:')}-{suffix}"
    if len(name.encode("ascii")) >= BOOT_ARTIFACT_NAME_MAX_BYTES:
        raise ValueError("boot-artifact volume name exceeds the supported dir-pool limit")
    return name


def parse_boot_artifact_name(name: str) -> BootArtifactName | None:
    """Recover a canonical version-1 identity, or return ``None`` for a foreign name."""
    match = _NAME.fullmatch(name)
    if match is None:
        return None
    try:
        system_id = UUID(match["system_id"])
        run_id = UUID(match["run_id"])
        attempt_id = UUID(match["attempt_id"]) if match["attempt_id"] is not None else None
    except ValueError:
        return None
    # UUID accepts several textual forms; require exact canonical re-rendering before ownership.
    if str(system_id) != match["system_id"] or str(run_id) != match["run_id"]:
        return None
    if attempt_id is not None and str(attempt_id) != match["attempt_id"]:
        return None
    kind = cast("BootArtifactKind", match["kind"])
    digest = "sha256:" + match["digest"]
    return BootArtifactName(
        name=name,
        kind=kind,
        system_id=system_id,
        run_id=run_id,
        digest=digest,
        partial=match["final"] is None,
        attempt_id=attempt_id,
    )


__all__ = [
    "BOOT_ARTIFACT_NAME_MAX_BYTES",
    "BootArtifactKind",
    "BootArtifactName",
    "parse_boot_artifact_name",
    "render_boot_artifact_name",
]
