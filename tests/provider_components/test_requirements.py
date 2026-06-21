from __future__ import annotations

import pytest

from kdive.components.requirements import (
    CmdlineRequirements,
    ConfigRequirements,
    _parse_config,
    validate_cmdline_requirements,
    validate_config_requirements,
)
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_config_requirements_accept_matching_values() -> None:
    validate_config_requirements(
        "CONFIG_VIRTIO_BLK=y\nCONFIG_DEBUG_INFO=y\n",
        ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y"}),
    )


def test_config_requirements_reject_missing_value() -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_config_requirements(
            "CONFIG_VIRTIO_BLK=n\n",
            ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y", "CONFIG_DEBUG_INFO": "y"}),
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(caught.value) == "kernel config does not satisfy profile requirements"
    # Both unsatisfied keys are reported, sorted, under the exact details key.
    assert caught.value.details == {
        "missing_or_different": ["CONFIG_DEBUG_INFO", "CONFIG_VIRTIO_BLK"]
    }


def test_config_disabled_line_parsed_as_n() -> None:
    # A "# CONFIG_X is not set" line means the option is disabled (value "n"); a profile
    # requiring it disabled must be satisfied, and requiring it enabled must fail.
    validate_config_requirements(
        "# CONFIG_VIRTIO_BLK is not set\n",
        ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "n"}),
    )
    with pytest.raises(CategorizedError):
        validate_config_requirements(
            "# CONFIG_VIRTIO_BLK is not set\n",
            ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y"}),
        )


def test_parse_config_keeps_value_with_embedded_equals() -> None:
    # Only the first '=' splits key from value; an embedded '=' in the value is preserved.
    parsed = _parse_config('CONFIG_CMDLINE="root=/dev/vda ro"\n')
    assert parsed == {"CONFIG_CMDLINE": '"root=/dev/vda ro"'}


def test_parse_config_ignores_disabled_marker_on_non_config_lines() -> None:
    # The disabled marker only applies to "# CONFIG_..." lines, not arbitrary comments.
    parsed = _parse_config("# something is not set\n")
    assert parsed == {}


def test_parse_config_ignores_config_line_without_equals() -> None:
    # A "CONFIG_..." line without '=' is not a key/value assignment and is skipped.
    parsed = _parse_config("CONFIG_VIRTIO_BLK\n")
    assert parsed == {}


def test_cmdline_requirements_accept_required_tokens() -> None:
    validate_cmdline_requirements(
        "console=ttyS0 root=/dev/vda dhash_entries=1",
        CmdlineRequirements(required_tokens=["console=ttyS0", "root=/dev/vda"]),
        platform_cmdline="console=ttyS0 root=/dev/vda",
    )


def test_cmdline_requirements_rejects_protected_override() -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_cmdline_requirements(
            "console=tty0 root=/dev/vda",
            CmdlineRequirements(required_tokens=["root=/dev/vda"], protected_prefixes=["console="]),
            platform_cmdline="console=ttyS0 root=/dev/vda",
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(caught.value) == "kernel command line overrides protected platform tokens"
    assert caught.value.details == {"protected_prefixes": ["console="]}


def test_cmdline_requirements_reject_missing_required_token() -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_cmdline_requirements(
            "console=ttyS0",
            CmdlineRequirements(required_tokens=["root=/dev/vda", "console=ttyS0"]),
            platform_cmdline="console=ttyS0",
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(caught.value) == "kernel command line does not include required tokens"
    assert caught.value.details == {"missing": ["root=/dev/vda"]}


def test_cmdline_supplied_prefix_absent_from_platform_is_not_an_override() -> None:
    # The platform has no token for this prefix, so a supplied token cannot override it.
    validate_cmdline_requirements(
        "debug=1 root=/dev/vda",
        CmdlineRequirements(required_tokens=["root=/dev/vda"], protected_prefixes=["debug="]),
        platform_cmdline="root=/dev/vda",
    )


def test_cmdline_platform_prefix_absent_from_supplied_is_not_an_override() -> None:
    # The platform protects a prefix the supplied cmdline omits; absence is not an override.
    validate_cmdline_requirements(
        "root=/dev/vda",
        CmdlineRequirements(required_tokens=["root=/dev/vda"], protected_prefixes=["console="]),
        platform_cmdline="console=ttyS0 root=/dev/vda",
    )
