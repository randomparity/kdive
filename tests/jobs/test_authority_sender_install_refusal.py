"""ADR-0814: the sender admits one sized refusal frame and ends the job on it."""

from __future__ import annotations

import json

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.authority_sender import _decode_response
from kdive.providers.external_boot_authority.protocol import AuthorityObservationV1

_SIZES = {"vmlinux_bytes": 3, "free_bytes": 1, "headroom_bytes": 2}


def _frame(**fields: object) -> bytes:
    return json.dumps(
        {"status": "error", **fields}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _raised(payload: bytes) -> CategorizedError:
    with pytest.raises(CategorizedError) as raised:
        _decode_response(payload, AuthorityObservationV1)
    return raised.value


def test_the_sized_frame_ends_the_job_as_install_failure_with_the_sizes() -> None:
    error = _raised(_frame(category="install-failure", install_refusal=_SIZES))

    assert error.category is ErrorCategory.INSTALL_FAILURE
    assert error.terminal is True
    assert error.details == {"install_refusal": _SIZES}
    assert str(error) == (
        "the guest filesystem has 1 bytes free but the 3-byte vmlinux needs 5 bytes including "
        "2 bytes of headroom; enlarge the System root disk by at least 4 bytes or build a "
        "smaller debuginfo"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"category": "install-failure"},
        {"category": "install-failure", "install_refusal": None},
        {"category": "install-failure", "install_refusal": {**_SIZES, "free_bytes": 5}},
        {"category": "install-failure", "install_refusal": {**_SIZES, "free_bytes": True}},
        {"category": "install-failure", "install_refusal": {**_SIZES, "free_bytes": 1.5}},
        {"category": "install-failure", "install_refusal": {**_SIZES, "free_bytes": 2**63}},
        {"category": "install-failure", "install_refusal": {**_SIZES, "directory": "/boot"}},
        {"category": "install-failure", "install_refusal": _SIZES, "detail": "text"},
        {"category": "provider-conflict", "install_refusal": _SIZES},
    ],
    ids=[
        "bare-category",
        "null-sizes",
        "not-a-shortfall",
        "bool",
        "float",
        "past-63-bits",
        "extra-size-key",
        "extra-frame-key",
        "sizes-on-another-category",
    ],
)
def test_any_other_shape_is_an_invalid_response(fields: dict[str, object]) -> None:
    error = _raised(_frame(**fields))

    assert error.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(error) == "authority: invalid-response"
    assert error.terminal is False


def test_the_device_identity_decoder_keeps_refusing_the_frame() -> None:
    from kdive.jobs.authority_sender import _PEER_REASONS

    assert "install-failure" not in _PEER_REASONS
