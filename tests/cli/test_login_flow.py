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
    assert "exp" not in captured


def test_mint_local_token_sets_exp_from_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ttl_seconds`` overrides the issuer's default expiry with ``now + ttl_seconds``."""
    import time

    issuer = OidcIssuer(base_url="http://issuer.example/default", audience="kdive")
    monkeypatch.setattr(login.OidcIssuer, "from_config", classmethod(lambda cls: issuer))
    captured: dict[str, object] = {}

    def _capture_code(got_issuer: OidcIssuer, claims: Mapping[str, object]) -> str:
        captured.update(claims)
        return "code"

    monkeypatch.setattr(login, "_authorization_code", _capture_code)
    monkeypatch.setattr(login, "_exchange_code", lambda got_issuer, code: "token")

    before = time.time()
    login.mint_local_token(project="local", ttl_seconds=43200)
    after = time.time()

    exp = captured["exp"]
    assert isinstance(exp, int)
    assert before + 43200 - 2 <= exp <= after + 43200 + 2


def test_mint_local_token_rejects_nonpositive_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-positive ``ttl_seconds`` fails fast rather than minting an already-dead token."""
    issuer = OidcIssuer(base_url="http://issuer.example/default", audience="kdive")
    monkeypatch.setattr(login.OidcIssuer, "from_config", classmethod(lambda cls: issuer))

    with pytest.raises(ValueError, match="ttl_seconds"):
        login.mint_local_token(project="local", ttl_seconds=0)


def test_mint_local_token_rejects_non_http_issuer_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = OidcIssuer(base_url="file:///tmp/issuer", audience="kdive")
    monkeypatch.setattr(login.OidcIssuer, "from_config", classmethod(lambda cls: issuer))

    def _unused_code(_issuer: OidcIssuer, _claims: Mapping[str, object]) -> str:
        raise AssertionError("authorization endpoint should not be called")

    def _unused_exchange(_issuer: OidcIssuer, _code: str) -> str:
        raise AssertionError("token endpoint should not be called")

    monkeypatch.setattr(login, "_authorization_code", _unused_code)
    monkeypatch.setattr(login, "_exchange_code", _unused_exchange)

    with pytest.raises(ValueError, match="http or https"):
        login.mint_local_token(project="local")


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_mint_local_token_accepts_http_and_https_issuer_schemes(
    monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    issuer = OidcIssuer(base_url=f"{scheme}://issuer.example/default", audience="kdive")
    monkeypatch.setattr(login.OidcIssuer, "from_config", classmethod(lambda cls: issuer))
    monkeypatch.setattr(login, "_authorization_code", lambda got_issuer, claims: "code")
    monkeypatch.setattr(login, "_exchange_code", lambda got_issuer, code: f"token-for-{code}")

    assert login.mint_local_token(project="local") == "token-for-code"


@pytest.mark.oidc_issuer
def test_mint_local_token_ttl_is_honored_by_the_live_issuer() -> None:
    """The injected ``exp`` overrides the issuer default end-to-end (the real round trip).

    The four stubbed unit tests above prove ``mint_local_token`` *injects* ``exp`` into the
    claims, but never that the issuer *honors* it — every one of them mocks the HTTP round
    trip. This is the gate for the documented per-request TTL: it mints against the live
    mock-OIDC server and decodes the issued JWT. If ``exp`` is ~3600 the issuer ignored the
    override and the documented 12h behavior is fiction (capped at the issuer's 1h default).
    """
    import time

    import jwt  # PyJWT: decode the issued token's claims without verifying the signature

    require_issuer()
    config.load()
    ttl = 120
    before = time.time()
    token = login.mint_local_token(project="local", ttl_seconds=ttl)
    after = time.time()

    decoded = jwt.decode(token, options={"verify_signature": False})
    exp = decoded["exp"]
    leeway = 30
    assert before + ttl - leeway <= exp <= after + ttl + leeway, (
        f"issued exp {exp} is not now+{ttl}s; the issuer ignored the injected expiry"
    )
    # The issuer's own default is 3600s — proving exp is near now+120 (not now+3600) is what
    # catches a silently-ignored override.
    assert abs(exp - (before + 3600)) > leeway, (
        f"issued exp {exp} is ~1h out: the issuer applied its default, not the requested TTL"
    )


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
