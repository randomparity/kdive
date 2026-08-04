"""Private error constructors shared by the GDB/MI implementation."""

from kdive.domain.errors import CategorizedError, ErrorCategory


def config_error(
    message: str, *, code: str, details: dict[str, object] | None = None
) -> CategorizedError:
    """Build a configuration error with the stable GDB/MI detail code."""
    merged: dict[str, object] = {"code": code, **(details or {})}
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, details=merged)
