"""The build profile is flat: there is no source discriminator or source-tree field."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import BuildProfile


def test_flat_profile_parses() -> None:
    parsed = BuildProfile.parse({"schema_version": 1})
    assert isinstance(parsed, BuildProfile)
    assert parsed.schema_version == 1


def test_source_field_is_rejected() -> None:
    # The lane collapsed to external-upload only: a leftover `source` discriminator is now an
    # unknown field.
    with pytest.raises(CategorizedError) as excinfo:
        BuildProfile.parse({"schema_version": 1, "source": "external"})
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_source_tree_field_is_rejected() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        BuildProfile.parse(
            {
                "schema_version": 1,
                "config": {"kind": "local", "path": "/configs/kernel.config"},
            }
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
