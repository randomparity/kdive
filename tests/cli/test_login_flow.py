"""Mock-OIDC ``kdivectl login`` flow (gated on the mock issuer, like the live-stack tests).

Marked ``oidc_issuer`` and guarded by ``require_issuer()`` so it runs only when the
mock-oauth2-server is up; otherwise it skips (it never un-gates the integration boundary).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

import kdive.config as config
from kdive.cli import login, transport
from kdive.cli.login import OidcIssuer
from kdive.config.cli_settings import CLI_CLIENT_ID, TOKEN
from tests.integration.live_stack.conftest import require_issuer


def test_mint_local_token_carries_project_role_and_platform_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mint_local_token`` builds project-role + platform-role claims and returns the token.

    The mock-OIDC round trip is stubbed so this is a pure unit test (no live issuer); it
    also guards the public symbol ``examples/local-libvirt/mint-token.sh`` imports.
    """
    issuer = OidcIssuer(base_url="http://issuer.example/default", audience="kdive")
    monkeypatch.setattr(login.OidcIssuer, "from_config", classmethod(lambda cls: issuer))
    captured: dict[str, object] = {}

    def _capture_code(got_issuer: OidcIssuer, claims: Mapping[str, object]) -> str:
        captured.update(claims)
        return "the-code"

    monkeypatch.setattr(login, "_authorization_code", _capture_code)
    monkeypatch.setattr(login, "_exchange_code", lambda got_issuer, code: f"token-for-{code}")

    token = login.mint_local_token(
        project="local", platform_roles=["platform_admin", "platform_operator"]
    )

    assert token == "token-for-the-code"
    assert captured["sub"] == "local-dev"
    assert captured["projects"] == ["local"]
    assert captured["roles"] == {"local": "admin"}
    assert captured["platform_roles"] == ["platform_admin", "platform_operator"]


def test_mint_local_token_omits_platform_roles_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``platform_roles``, the claim is omitted entirely (not an empty list)."""
    issuer = OidcIssuer(base_url="http://issuer.example/default", audience="kdive")
    monkeypatch.setattr(login.OidcIssuer, "from_config", classmethod(lambda cls: issuer))
    captured: dict[str, object] = {}

    def _capture_code(got_issuer: OidcIssuer, claims: Mapping[str, object]) -> str:
        captured.update(claims)
        return "code"

    monkeypatch.setattr(login, "_authorization_code", _capture_code)
    monkeypatch.setattr(login, "_exchange_code", lambda got_issuer, code: "token")

    login.mint_local_token(project="demo", role="viewer")

    assert captured["roles"] == {"demo": "viewer"}
    assert "platform_roles" not in captured


@pytest.mark.oidc_issuer
def test_login_mints_platform_admin_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_issuer()
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "token")
    config.load()
    token = login.login(platform_role="platform_admin")
    assert token
    assert login.read_cached_token() == token
    assert stat.S_IMODE(os.stat(tmp_path / "token").st_mode) == 0o600


@pytest.mark.oidc_issuer
def test_login_without_platform_role_still_mints_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_issuer()
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "token")
    config.load()
    token = login.login(platform_role=None)
    assert token
    assert login.read_cached_token() == token


@pytest.mark.oidc_issuer
def test_session_picks_up_login_cache_when_token_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_issuer()
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "token")
    monkeypatch.delenv(TOKEN.name, raising=False)
    config.load()
    token = login.login(platform_role="platform_operator")
    session = transport.Session.from_env()
    assert session.token == token


@pytest.mark.oidc_issuer
def test_login_sets_azp_from_cli_client_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    require_issuer()
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "token")
    monkeypatch.setenv(CLI_CLIENT_ID.name, "kdivectl")
    config.load()
    captured: dict[str, object] = {}
    real_authorization_code = login._authorization_code

    def _capture(issuer: OidcIssuer, claims: Mapping[str, object]) -> str:
        captured.update(claims)
        return real_authorization_code(issuer, claims)

    monkeypatch.setattr(login, "_authorization_code", _capture)
    login.login(platform_role="platform_admin")
    assert captured["azp"] == "kdivectl"
    assert captured["platform_roles"] == ["platform_admin"]
