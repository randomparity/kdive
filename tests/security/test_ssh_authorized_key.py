"""Tests for the agent-supplied SSH public-key validator (ADR-0271, #782)."""

from __future__ import annotations

import base64

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.ssh_authorized_key import validate_authorized_public_key

_BLOB = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519abcdefghij").decode()


def _ed25519(comment: str = "agent@host") -> str:
    return f"ssh-ed25519 {_BLOB} {comment}"


def test_accepts_ed25519_with_comment() -> None:
    assert validate_authorized_public_key(f"  {_ed25519()}\n") == _ed25519()


def test_accepts_rsa_and_ecdsa_without_comment() -> None:
    assert validate_authorized_public_key(f"ssh-rsa {_BLOB}") == f"ssh-rsa {_BLOB}"
    assert (
        validate_authorized_public_key(f"ecdsa-sha2-nistp256 {_BLOB}")
        == f"ecdsa-sha2-nistp256 {_BLOB}"
    )


def test_accepts_sk_security_key_type() -> None:
    key = f"sk-ssh-ed25519@openssh.com {_BLOB} token"
    assert validate_authorized_public_key(key) == key


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        _BLOB,  # no key-type token
        "ssh-ed25519 not_base64!!",  # blob not valid base64
        f'command="rm -rf /" ssh-ed25519 {_BLOB}',  # authorized_keys options smuggled
        f"ssh-ed25519 {_BLOB}\nssh-ed25519 {_BLOB}",  # multi-line
        f"ssh-ed25519 {_BLOB}\x07 comment",  # control char in comment
        f"ssh-ed25519 {_BLOB} " + "x" * 9000,  # over the length cap
        f"ssh-dss {_BLOB}",  # disallowed (weak) key type
        f"no-pty ssh-ed25519 {_BLOB}",  # options keyword before the type
    ],
)
def test_rejects_malformed(bad: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        validate_authorized_public_key(bad)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "invalid_public_key"
