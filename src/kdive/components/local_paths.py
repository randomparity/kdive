"""Provider-local component path validation (ADR-0065)."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory


def validate_local_component_path(
    path: str,
    *,
    allowed_roots: Iterable[Path],
    sha256: str | None = None,
) -> Path:
    """Return a resolved regular file path after provider-root and digest validation."""
    candidate = Path(path)
    if not candidate.is_absolute():
        raise _config_error("local component path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _config_error("local component path does not exist") from exc

    roots = [root.resolve(strict=False) for root in allowed_roots]
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        # Surface the configured roots so a black-box MCP caller can self-correct without host
        # access (#731, ADR-0224). Sorted for a stable wire order; only operator-configured
        # roots, never the caller-submitted path or a secret (no-leak, ADR-0123).
        raise _config_error(
            "local component path is outside provider allowed roots",
            details={"accepted_values": sorted(str(root) for root in roots)},
        )
    if not resolved.is_file():
        raise _config_error("local component path is not a regular file")
    if not os.access(resolved, os.R_OK):
        raise _config_error("local component path is not readable")
    if sha256 is not None:
        try:
            actual = _file_sha256(resolved)
        except OSError as exc:
            raise _config_error("local component sha256 could not be read") from exc
        if actual != sha256.removeprefix("sha256:"):
            raise _config_error("local component sha256 does not match")
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _config_error(message: str, *, details: dict[str, object] | None = None) -> CategorizedError:
    return CategorizedError(
        message, category=ErrorCategory.CONFIGURATION_ERROR, details=details or {}
    )
