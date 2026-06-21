"""Tests for the shared git-source validator + local-build remote allowlist (ADR-0162)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.build_host.configuration.git_source import (
    parse_remote,
    remote_allowed,
    validate_git_arg,
)

# --- validate_git_arg (relocated from shell_transport) ------------------------------


def test_validate_git_arg_rejects_leading_dash() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_git_arg("--upload-pack=evil", "remote")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"field": "remote"}
    assert "git option" in str(exc.value)


def test_validate_git_arg_rejects_control_char() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_git_arg("https://github.com/x\n", "ref")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"field": "ref"}
    assert str(exc.value) == "ref contains a control character or newline"


def test_validate_git_arg_accepts_plain() -> None:
    validate_git_arg("https://github.com/torvalds/linux", "remote")


# --- parse_remote (scheme/host/path extraction) ------------------------------------


def test_parse_remote_url_form_splits_scheme_host_path() -> None:
    assert parse_remote("https://github.com/myorg/linux") == (
        "https",
        "github.com",
        "/myorg/linux",
    )


def test_parse_remote_lowercases_host_and_strips_userinfo_and_port() -> None:
    assert parse_remote("ssh://git@GitHub.com:22/myorg/linux") == (
        "ssh",
        "github.com",
        "/myorg/linux",
    )


def test_parse_remote_url_without_host_yields_empty_host() -> None:
    assert parse_remote("https:///path") == ("https", "", "/path")


def test_parse_remote_url_without_path_yields_empty_path() -> None:
    assert parse_remote("git://host") == ("git", "host", "")


def test_parse_remote_scp_form_normalizes_to_ssh_with_leading_slash() -> None:
    assert parse_remote("git@git.example.com:team/linux.git") == (
        "ssh",
        "git.example.com",
        "/team/linux.git",
    )


def test_parse_remote_scp_form_splits_only_on_first_colon() -> None:
    # A path may itself contain a colon; only the first colon separates host from path.
    assert parse_remote("host:1234:p") == ("ssh", "host", "/1234:p")


def test_parse_remote_scp_form_strips_only_last_userinfo_at() -> None:
    # The host is the segment after the final '@', so a stray '@' before it is dropped.
    assert parse_remote("a@b@host:path") == ("ssh", "host", "/path")


def test_parse_remote_slash_before_first_colon_is_not_scp() -> None:
    # A '/' before the first colon means the colon is inside a path, not a host:path
    # separator, so the scp branch must not fire (it would mis-read "a/b" as a host).
    assert parse_remote("a/b:c") == ("", "", "")


def test_parse_remote_scp_host_uses_segment_before_first_colon_only() -> None:
    # Only slashes in the host segment (before the first colon) disqualify scp form; a
    # later colon-bearing path is still valid scp form keyed on the first colon.
    assert parse_remote("host:a/b:c") == ("ssh", "host", "/a/b:c")


def test_parse_remote_unparseable_yields_empty_triple() -> None:
    assert parse_remote("noremote") == ("", "", "")


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
