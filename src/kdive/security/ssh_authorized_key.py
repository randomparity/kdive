"""Validate an agent-supplied SSH public key before it is authorized in a guest (ADR-0271).

The validator is the trust boundary for a root-granting ``authorized_keys`` append: it rejects
anything that is not a single, well-formed public-key line, so an agent cannot smuggle an
``authorized_keys`` options/command field, extra authorized lines, or control characters into the
guest's ``/root/.ssh/authorized_keys``.
"""

from __future__ import annotations

import base64
import binascii

from kdive.domain.errors import CategorizedError, ErrorCategory

#: Public-key algorithms an agent may authorize. Weak/legacy types (``ssh-dss``) are excluded.
_ALLOWED_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)
_MAX_LEN = 8 * 1024


def _reject(detail: str) -> CategorizedError:
    return CategorizedError(
        detail,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": "invalid_public_key"},
    )


def _has_control_character(value: str) -> bool:
    return any((ord(ch) < 0x20 and ch not in "\r\n") or ord(ch) == 0x7F for ch in value)


def validate_authorized_public_key(raw: str) -> str:
    """Return the normalized one-line public key, or raise ``CONFIGURATION_ERROR``.

    Args:
        raw: The agent-supplied public key, exactly as received.

    Returns:
        The stripped, single-line key suitable for appending to ``authorized_keys``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` (``reason=invalid_public_key``) when the input
            exceeds the length cap, contains a control character, is empty or multi-line, lacks a
            type token and key blob, leads with a non-allow-listed token (an options/command field
            or a weak algorithm), or carries a non-base64 key blob.
    """
    if len(raw) > _MAX_LEN:
        raise _reject("public key exceeds the maximum length")
    if _has_control_character(raw):
        raise _reject("public key contains a control character")
    line = raw.strip()
    if not line or "\n" in line or "\r" in line:
        raise _reject("public key must be exactly one non-empty line")
    fields = line.split()
    if len(fields) < 2:
        raise _reject("public key must have a type token and a key blob")
    key_type, blob = fields[0], fields[1]
    if key_type not in _ALLOWED_KEY_TYPES:
        raise _reject(f"unsupported or non-key-type leading token: {key_type!r}")
    try:
        base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _reject("public key blob is not valid base64") from exc
    return line


__all__ = ["validate_authorized_public_key"]
