"""Tests for the shared git-source validator + local-build remote allowlist (ADR-0161)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.build_host.git_source import (
    remote_allowed,
    validate_git_arg,
)

# --- validate_git_arg (relocated from shell_transport) ------------------------------


def test_validate_git_arg_rejects_leading_dash() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_git_arg("--upload-pack=evil", "remote")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_git_arg_rejects_control_char() -> None:
    with pytest.raises(CategorizedError):
        validate_git_arg("https://github.com/x\n", "remote")


def test_validate_git_arg_accepts_plain() -> None:
    validate_git_arg("https://github.com/torvalds/linux", "remote")


# --- remote_allowed (allowlist matching) -------------------------------------------

_ALLOW = ("github.com/myorg", "git.example.com")


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/myorg/linux",
        "https://github.com/myorg/linux.git",
        "https://GitHub.com/myorg/linux",  # host case-insensitive
        "git@git.example.com:team/linux.git",  # scp-like, host-only entry
        "ssh://git.example.com/team/linux",
        "git://git.example.com/team/linux",
    ],
)
def test_remote_allowed_accepts(remote: str) -> None:
    assert remote_allowed(remote, _ALLOW) is True


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com.evil.com/myorg/linux",  # not a substring match
        "https://github.com/myorg-evil/linux",  # path boundary
        "https://github.com/other/linux",  # wrong path on a path-scoped entry
        "https://gitlab.com/myorg/linux",  # host not listed
        "file:///etc/passwd",  # scheme rejected
        "http://git.example.com/team/linux",  # http not eligible
        "ext::sh -c id",  # helper transport / not a host
        "",
    ],
)
def test_remote_allowed_rejects(remote: str) -> None:
    assert remote_allowed(remote, _ALLOW) is False


def test_remote_allowed_empty_allowlist_denies_all() -> None:
    assert remote_allowed("https://github.com/myorg/linux", ()) is False
