"""Unit tests for the network-free parts of the wire harness (claims + issuer config)."""

from __future__ import annotations

import pytest

from kdive.mcp.dev_harness import (
    OidcIssuer,
    _build_claims,
    oidc_issuer_from_env,
)
from tests.integration.live_stack.spine import raw_vmcore_refs


def test_build_claims_nested_roles_object() -> None:
    claims = _build_claims(
        subject="admin-proj-a",
        audience="kdive",
        projects=["proj-a"],
        roles={"proj-a": "admin"},
        platform_roles=None,
        agent_session="sess-1",
    )
    assert claims["sub"] == "admin-proj-a"
    assert claims["aud"] == "kdive"
    assert claims["projects"] == ["proj-a"]
    assert claims["roles"] == {"proj-a": "admin"}  # nested object, not flat
    assert claims["agent_session"] == "sess-1"
    assert "platform_roles" not in claims  # None -> omitted


def test_build_claims_platform_roles_array() -> None:
    claims = _build_claims(
        subject="auditor",
        audience="kdive",
        projects=["proj-a"],
        roles={"proj-a": "viewer"},
        platform_roles=["platform_auditor"],
        agent_session=None,
    )
    assert claims["platform_roles"] == ["platform_auditor"]  # flat array
    assert "agent_session" not in claims  # None -> omitted


def test_build_claims_empty_platform_roles_is_present_but_empty() -> None:
    claims = _build_claims(
        subject="x",
        audience="kdive",
        projects=[],
        roles={},
        platform_roles=[],
        agent_session=None,
    )
    assert claims["platform_roles"] == []  # [] -> present-but-empty, distinct from None
    assert claims["projects"] == []
    assert claims["roles"] == {}


def test_oidc_issuer_derived_endpoints() -> None:
    issuer = OidcIssuer(
        base_url="http://localhost:8090/default", audience="kdive", client_id="kdive-test"
    )
    assert issuer.authorize_endpoint == "http://localhost:8090/default/authorize"
    assert issuer.token_endpoint == "http://localhost:8090/default/token"
    assert issuer.jwks_uri == "http://localhost:8090/default/jwks"


def test_oidc_issuer_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_OIDC_ISSUER", "http://localhost:8090/default")
    monkeypatch.setenv("KDIVE_OIDC_AUDIENCE", "kdive")
    monkeypatch.delenv("KDIVE_OIDC_CLIENT_ID", raising=False)
    issuer = oidc_issuer_from_env()
    assert issuer.base_url == "http://localhost:8090/default"
    assert issuer.audience == "kdive"
    assert issuer.client_id == "kdive-test"  # default


def test_oidc_issuer_from_env_missing_base_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_OIDC_ISSUER", raising=False)
    with pytest.raises(RuntimeError, match="KDIVE_OIDC_ISSUER"):
        oidc_issuer_from_env()


# --- the raw-vmcore egress check (#1610) ----------------------------------------------------
#
# The first live run of the #1609 assertions reported "raw vmcore leaked" on a correctly
# REDACTED core: the check tested the whole ref string, and a presigned URL ends with its
# signing query, not with `-redacted`. These pin both directions so the fix cannot regress into
# either a false positive or a check that no longer catches a real leak.

_RED_KEY = "remote-libvirt/runs/r1/vmcore-kdump-redacted"
_RED_URL = f"http://s3.example:30900/kdive-artifacts/{_RED_KEY}?AWSAccessKeyId=k&Signature=s"
_RAW_KEY = "remote-libvirt/runs/r1/vmcore-kdump"
_RAW_URL = f"http://s3.example:30900/kdive-artifacts/{_RAW_KEY}?AWSAccessKeyId=k&Signature=s"


def test_redacted_presigned_url_is_not_a_leak() -> None:
    # The exact shape artifacts.get returns, and the exact false positive seen live.
    assert raw_vmcore_refs([_RED_URL]) == []


def test_redacted_object_key_is_not_a_leak() -> None:
    assert raw_vmcore_refs([_RED_KEY]) == []


def test_an_opaque_artifact_id_is_not_a_leak() -> None:
    assert raw_vmcore_refs(["a5a050ce-b055-42c6-b6cf-4a823623042b"]) == []


def test_a_raw_object_key_is_still_caught() -> None:
    assert raw_vmcore_refs([_RAW_KEY]) == [_RAW_KEY]


def test_a_raw_presigned_url_is_still_caught() -> None:
    # The check must survive the query string in BOTH directions, not just the passing one.
    assert raw_vmcore_refs([_RAW_URL]) == [_RAW_URL]


def test_a_raw_ref_among_redacted_ones_is_caught() -> None:
    assert raw_vmcore_refs([_RED_KEY, _RAW_KEY, _RED_URL]) == [_RAW_KEY]
