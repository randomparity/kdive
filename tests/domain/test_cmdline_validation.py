"""Direct tests for caller-controlled kernel cmdline validation."""

import pytest

from kdive.domain.cmdline import MAX_CMDLINE_EXTRA_LENGTH, cmdline_extra_error


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, None),
        (" panic=1 ", None),
        (" ", "cmdline_blank"),
        ("x" * (MAX_CMDLINE_EXTRA_LENGTH + 1), "cmdline_too_long"),
        ("panic=1\x00", "cmdline_not_printable"),
    ],
)
def test_cmdline_extra_error(value: str | None, reason: str | None) -> None:
    assert cmdline_extra_error(value) == reason
