"""Content-addressed object naming for the investigation-scoped uploaded rootfs (ADR-0441).

An investigation-scoped uploaded rootfs is content-addressed: its object name embeds a
path/key-safe rendering (unpadded base64url) of the declared SHA-256, so one checksum maps to
exactly one object key. This module is the **single** derivation both the upload tool
(``mcp.tools.catalog.artifacts.uploads``) and the provider fetch
(``providers.local_libvirt.lifecycle.rootfs.rootfs_upload_fetch``) share, so a System resolves the
exact key finalize committed. The provider layer must not import from ``kdive.mcp.tools``, so the
derivation lives here in ``kdive.artifacts`` where both layers may depend on it.
"""

from __future__ import annotations

import base64
import binascii

from kdive.domain.errors import CategorizedError, ErrorCategory

# The object-name prefix content-addressing the investigation rootfs; ``rootfs-<token>``.
_ROOTFS_OBJECT_PREFIX = "rootfs-"


def rootfs_object_token(sha256_b64: str) -> str:
    """Return the unpadded base64url token for a canonical-base64 SHA-256 checksum.

    The investigation-scoped uploaded rootfs is content-addressed: the object name embeds this
    token so one checksum maps to exactly one key. base64url is path/key-safe (no ``/``), and the
    mapping is total and reversible against the canonical base64 the object store stores.

    Args:
        sha256_b64: The declared checksum as canonical (padded) base64 of a 32-byte digest.

    Returns:
        The unpadded base64url rendering of the same digest.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``sha256_b64`` is not valid base64 of a
            32-byte digest — rejected rather than deriving an un-resolvable key.
    """
    try:
        raw = base64.b64decode(sha256_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CategorizedError(
            "rootfs checksum_sha256 is not valid base64",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc
    if len(raw) != 32:
        raise CategorizedError(
            "rootfs checksum_sha256 must be a base64-encoded SHA-256 (32 bytes)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def rootfs_object_name(token: str) -> str:
    """Return the content-addressed object name (``rootfs-<token>``) for a rootfs token."""
    return f"{_ROOTFS_OBJECT_PREFIX}{token}"
