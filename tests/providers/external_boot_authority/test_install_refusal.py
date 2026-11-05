"""ADR-0814: the only provider facts an authority error frame carries are three bounded sizes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kdive.providers.external_boot_authority.protocol import AuthorityInstallRefusalV1
from kdive.providers.external_boot_authority.service import AuthorityServiceError

_MAX = 2**63 - 1
_VALID = {"vmlinux_bytes": 3, "free_bytes": 1, "headroom_bytes": 2}


def test_a_shortfall_is_accepted_up_to_the_bound() -> None:
    assert AuthorityInstallRefusalV1.model_validate(_VALID).model_dump() == _VALID
    AuthorityInstallRefusalV1.model_validate(
        {"vmlinux_bytes": _MAX, "free_bytes": _MAX, "headroom_bytes": _MAX}
    )


@pytest.mark.parametrize(
    "value",
    [
        {**_VALID, "free_bytes": True},
        {**_VALID, "free_bytes": 1.0},
        {**_VALID, "free_bytes": "1"},
        {**_VALID, "free_bytes": -1},
        {**_VALID, "vmlinux_bytes": 2**63},
        {**_VALID, "directory": "/boot"},
        {"vmlinux_bytes": 3, "headroom_bytes": 2},
        {**_VALID, "free_bytes": 5},
        {**_VALID, "free_bytes": 6},
    ],
    ids=[
        "bool",
        "float",
        "string",
        "negative",
        "past-63-bits",
        "extra-key",
        "missing-key",
        "exact-fit",
        "room-to-spare",
    ],
)
def test_anything_but_a_bounded_shortfall_is_refused(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AuthorityInstallRefusalV1.model_validate(value)


def test_the_category_and_the_sizes_are_a_pair() -> None:
    sizes = AuthorityInstallRefusalV1.model_validate(_VALID)

    assert AuthorityServiceError("install_failure", install_refusal=sizes).install_refusal == sizes
    with pytest.raises(ValueError, match="pair"):
        AuthorityServiceError("install_failure")
    with pytest.raises(ValueError, match="pair"):
        AuthorityServiceError("provider_conflict", install_refusal=sizes)
