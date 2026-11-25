"""Framework validation wrappers for caller-provenance proofs."""

from __future__ import annotations

from fastmcp.exceptions import ValidationError as FastMCPValidationError


def wrap_validation_error(cause: Exception | None) -> FastMCPValidationError:
    """Build the exact framework wrapper/cause identity for focused provenance fixtures."""
    error = FastMCPValidationError("caller validation")
    error.__cause__ = cause
    return error
