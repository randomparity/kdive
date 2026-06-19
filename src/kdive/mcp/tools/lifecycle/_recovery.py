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


def _provenance(source: str, kernel_source_ref: object) -> str:
    """Derive the source provenance label without echoing the reference itself."""
    if source == "external":
        return "external"
    if isinstance(kernel_source_ref, Mapping) and "git" in kernel_source_ref:
        return "git"
    return "warm-tree"


def build_profile_summary(profile: Mapping[str, object]) -> dict[str, JsonValue]:
    """Return the allowlisted build summary: source lane, host, and derived provenance."""
    raw_source = profile.get("source", "server")
    source = raw_source if isinstance(raw_source, str) else "server"
    summary: dict[str, JsonValue] = {"build_source": source}
    host = profile.get("build_host")
    if isinstance(host, str):
        summary["build_host"] = host
    summary["build_source_provenance"] = _provenance(source, profile.get("kernel_source_ref"))
    return summary


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
