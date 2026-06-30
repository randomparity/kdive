"""Client-attested source provenance for external-build uploads (ADR-0274, #893).

An external build (``source="external"``) uploads a prebuilt kernel artifact; KDIVE does not
build from, clone, or verify any source tree on that lane. An agent may still record *which*
local source tree/ref produced the artifacts by passing a freeform ``source_label`` / ``source_ref``
to ``runs.complete_build``. This helper validates that claim and assembles the map recorded into
the existing ``build_provenance`` (``BuildStepResult.build_provenance``), surfaced verbatim by
``runs.get``.

The recorded map carries ``client_attested: true`` — a positive discriminator present only on this
client-asserted lane, never the server build's KDIVE-captured provenance — so a reader can tell
KDIVE did not build or verify the named tree. The fields are the caller's own input echoed back to
its own project read (same posture as ``label``, ADR-0264), so they are length/character-validated
here rather than run through the secret redactor; they are opaque labels, never cloned or resolved.
"""

from __future__ import annotations

from kdive.domain.errors import CategorizedError, ErrorCategory

PROVENANCE_FIELD_MAX_LEN = 256
"""Maximum length, in Unicode code points, of a source-provenance field."""

CLIENT_ATTESTED_KEY = "client_attested"
"""The positive discriminator marking the map as an unverified client claim."""

_INVALID_REASON = "invalid_source_provenance"


def _reject(field: str) -> CategorizedError:
    """Build the uniform invalid-provenance error, naming the rule and field but not the value."""
    return CategorizedError(
        f"{field} must be 1..{PROVENANCE_FIELD_MAX_LEN} printable characters",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": _INVALID_REASON, "field": field},
    )


def _clean(value: str | None, *, field: str) -> str | None:
    """Strip ``value``; return ``None`` when empty after strip, else the validated value."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not (1 <= len(cleaned) <= PROVENANCE_FIELD_MAX_LEN):
        raise _reject(field)
    if not cleaned.isprintable():
        raise _reject(field)
    return cleaned


def external_source_provenance(
    source_label: str | None, source_ref: str | None
) -> dict[str, str | bool] | None:
    """Validate and assemble a client-attested source-provenance claim.

    Args:
        source_label: A freeform human handle for the source tree, or ``None``.
        source_ref: The ref/commit the agent claims produced the artifacts, or ``None``.

    Returns:
        ``None`` when neither argument yields a non-empty value (``build_provenance`` stays unset),
        else ``{"client_attested": True, "label"?: ..., "source_ref"?: ...}`` with only the
        supplied, validated fields present.

    Raises:
        CategorizedError: ``configuration_error`` (``details == {"reason":
            "invalid_source_provenance", "field": "source_label" | "source_ref"}``) when a supplied
            value is longer than ``PROVENANCE_FIELD_MAX_LEN`` code points or contains a
            non-printable character. The message and details name the rule and field, never the
            rejected value (ADR-0123).
    """
    label = _clean(source_label, field="source_label")
    ref = _clean(source_ref, field="source_ref")
    if label is None and ref is None:
        return None
    provenance: dict[str, str | bool] = {CLIENT_ATTESTED_KEY: True}
    if label is not None:
        provenance["label"] = label
    if ref is not None:
        provenance["source_ref"] = ref
    return provenance
