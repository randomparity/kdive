"""Shared GDB/MI error-construction contract tests."""

from kdive.domain.errors import ErrorCategory
from kdive.providers.shared.debug_common.gdbmi._errors import config_error


def test_config_error_adds_code_and_preserves_details() -> None:
    error = config_error("bad command", code="bad_command", details={"command": "unsafe"})

    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert error.details == {"code": "bad_command", "command": "unsafe"}
