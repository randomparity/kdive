"""The volume-name grammar that carries remote module volume ownership (ADR-0588).

libvirt does not persist a `<metadata>` element on a storage volume — it accepts one and
discards it silently — so ADR-0588 makes the volume *name* the durable ownership channel.
This module is the single place in `src/` that renders or recognises a module volume name.

Recognition is one anchored `re.fullmatch` of the whole name. A name that does not match is a
foreign volume: never read further, never deleted, never counted. There is no prefix match and
no partial credit, because a prefix match gives an operator's volume partial ownership.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MODULE_VOLUME_NAME_MAX_BYTES = 255
MODULE_VOLUME_KINDS = ("source.ext4", "scratch.ext4", "reaping.journal", "reaped.journal")

_PREFIX = "kdive-module-"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_NONCE = r"[0-9a-f]{32}"
_KIND = "|".join(re.escape(kind) for kind in MODULE_VOLUME_KINDS)
_NAME = re.compile(
    rf"{re.escape(_PREFIX)}(?P<system_id>{_UUID})-(?P<run_id>{_UUID})"
    rf"-(?P<operation_nonce>{_NONCE})-(?P<kind>{_KIND})"
)
_UUID_ONLY = re.compile(_UUID)
_NONCE_ONLY = re.compile(_NONCE)


@dataclass(frozen=True, slots=True)
class ModuleVolumeOwner:
    """The owner tuple a module volume name carries, recovered from the name alone."""

    system_id: str
    run_id: str
    operation_nonce: str
    kind: str


def render_module_volume_name(system_id: str, run_id: str, operation_nonce: str, kind: str) -> str:
    """Render the volume name for one attempt-scoped module volume.

    Raises `ValueError` on any input the grammar cannot express, rather than emitting a name
    that would parse back as foreign and leak.
    """
    for label, value, pattern in (
        ("system id", system_id, _UUID_ONLY),
        ("run id", run_id, _UUID_ONLY),
        ("operation nonce", operation_nonce, _NONCE_ONLY),
    ):
        if not pattern.fullmatch(value):
            raise ValueError(f"module volume {label} is not canonical lowercase: {value!r}")
    if kind not in MODULE_VOLUME_KINDS:
        raise ValueError(f"module volume kind is not one of {MODULE_VOLUME_KINDS}: {kind!r}")
    return f"{_PREFIX}{system_id}-{run_id}-{operation_nonce}-{kind}"


def parse_module_volume_name(name: str) -> ModuleVolumeOwner | None:
    """Recover the owner tuple from a volume name, or `None` if the name is not ours."""
    match = _NAME.fullmatch(name)
    if match is None:
        return None
    return ModuleVolumeOwner(**match.groupdict())
