"""Client-supplied label validation (ADR-0264, #867).

A ``label`` is an optional, freeform, non-unique human handle a caller may attach to a
Run or System to cut bare-UUID threading. It is the caller's own input, echoed back like
``investigations.title`` — not machine output — so it is length- and character-validated
here rather than run through the secret redactor.

Validation lives in this pure helper (called from the handler/service layer) rather than
as a pydantic ``Field`` bound: ``runs.create`` / ``systems.define`` / ``systems.provision``
sit behind ``BindingErrorMiddleware`` whose conversions match only profile errors, so a
schema bound would leak a raw ``ValidationError`` instead of the uniform envelope
(ADR-0247 / ADR-0259).
"""

from __future__ import annotations

from kdive.domain.errors import CategorizedError, ErrorCategory

LABEL_MAX_LEN = 200
"""Maximum label length in Unicode code points."""

_INVALID_LABEL_REASON = "invalid_label"


def _reject() -> CategorizedError:
    """Build the uniform invalid-label error, naming the rule but never the value."""
    return CategorizedError(
        f"label must be 1..{LABEL_MAX_LEN} printable characters",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": _INVALID_LABEL_REASON},
    )


def validate_label(label: str | None) -> str | None:
    """Validate and normalize an optional client label.

    Args:
        label: The caller-supplied label, or ``None`` for no label.

    Returns:
        ``None`` when no label was supplied, else the surrounding-whitespace-stripped
        label.

    Raises:
        CategorizedError: ``configuration_error`` (``details["reason"] ==
            "invalid_label"``) when the stripped label is empty, longer than
            ``LABEL_MAX_LEN`` code points, or contains a non-printable character
            (control, format/zero-width/bidi, surrogate, or non-``U+0020`` separator).
            The message and details name the rule only — never the rejected value
            (ADR-0123).
    """
    if label is None:
        return None
    cleaned = label.strip()
    if not (1 <= len(cleaned) <= LABEL_MAX_LEN):
        raise _reject()
    if not cleaned.isprintable():
        raise _reject()
    return cleaned
