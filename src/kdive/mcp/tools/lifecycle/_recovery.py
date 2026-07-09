"""Redaction-safe recovery summaries for lifecycle get/list envelopes (#568, ADR-0180).

These helpers extract an allowlisted summary from a stored profile document. They read
fields with ``.get()`` (never re-parse the profile) so a read tool cannot raise on a
slightly-off stored document, and they echo only enumerated discriminators, registry
identifiers, and sizing integers — never a free-form reference string that could carry an
inline credential.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from kdive.serialization import JsonValue


def iso(dt: datetime | None) -> str | None:
    """Serialize a datetime to ISO-8601, passing ``None`` through."""
    return dt.isoformat() if dt is not None else None


def build_profile_summary(_profile: Mapping[str, object]) -> dict[str, JsonValue]:
    """Return the allowlisted build summary.

    Every Run is the external-upload lane (the agent builds locally and uploads), so the
    summary is a fixed ``build_source`` marker; no source-tree or host field is derived.
    """
    return {"build_source": "external"}


def provisioning_profile_summary(profile: Mapping[str, object]) -> dict[str, JsonValue]:
    """Return the allowlisted provisioning summary: arch, boot method, and sizing."""
    summary: dict[str, JsonValue] = {}
    for key in ("arch", "boot_method"):
        value = profile.get(key)
        if isinstance(value, str):
            summary[key] = value
    for key in ("vcpu", "memory_mb", "disk_gb"):
        value = profile.get(key)
        # bool is an int subclass; exclude it explicitly so a stray bool isn't surfaced.
        if isinstance(value, int) and not isinstance(value, bool):
            summary[key] = value
    return summary
