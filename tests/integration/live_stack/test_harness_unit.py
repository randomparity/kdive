"""Unit tests for the network-free parts of the wire harness (claims + issuer config)."""

from __future__ import annotations

import pytest

from kdive.mcp.dev_harness import (
    OidcIssuer,
    _build_claims,
    oidc_issuer_from_env,
)
from tests.integration.live_stack.spine import (
    assembled_console_refs,
    assert_no_live_secrets,
    raw_vmcore_refs,
)


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


# --- assembled_console_refs (ADR-0095, #1610) ------------------------------------------------
#
# The capstone's console phase asserted `endswith("/console")` against what it assumed was a
# list of artifacts; `artifacts.list` answers with ONE envelope carrying them in `items`, so the
# phase failed on shape before any ref was read. These pin the predicate that replaced it: a
# System listing carries the open stream and its part chunks alongside the assembled artifact,
# and only the last of those proves the collector spanned the crash.

_RUN = "e95bea84-9ec0-4e45-bff0-cee7c834fbec"
_SYS_PREFIX = "remote-libvirt/systems/ee87192f-9280-4b8a-b0a3-9fdf25f1e2b8"
_CONSOLE_STREAM = f"{_SYS_PREFIX}/console"
_CONSOLE_PART = f"{_SYS_PREFIX}/console-part-0-000003"
_CONSOLE_DONE = f"{_SYS_PREFIX}/console-{_RUN}"


def test_the_assembled_console_artifact_is_found() -> None:
    # The exact three-ref listing the first live capstone run returned.
    refs = [_CONSOLE_STREAM, _CONSOLE_PART, _CONSOLE_DONE]
    assert assembled_console_refs(refs, _RUN) == [_CONSOLE_DONE]


def test_the_open_stream_alone_does_not_satisfy_the_phase() -> None:
    # What the naive `endswith("/console")` matched — present even if finalize never ran.
    assert assembled_console_refs([_CONSOLE_STREAM, _CONSOLE_PART], _RUN) == []


def test_another_runs_assembled_console_does_not_satisfy_the_phase() -> None:
    other = f"{_SYS_PREFIX}/console-11111111-2222-3333-4444-555555555555"
    assert assembled_console_refs([_CONSOLE_STREAM, other], _RUN) == []


def test_the_assembled_console_survives_a_presigned_query() -> None:
    url = f"http://s3.example:30900/kdive-artifacts/{_CONSOLE_DONE}?AWSAccessKeyId=k&Signature=s"
    assert assembled_console_refs([url], _RUN) == [url]


# --- assert_no_live_secrets (#1610) ----------------------------------------------------------
#
# The check this replaced searched the postmortem report for the literal "hunter2" — a fixture
# value from unrelated unit tests, planted nowhere in the remote spine — so it passed however
# badly the report leaked. The replacement hunts the values the stack is actually running with,
# and refuses to run at all when there are none, so it cannot decay back into a no-op.


def test_a_leaked_live_secret_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "kdive-demo-secret")
    with pytest.raises(AssertionError, match="AWS_SECRET_ACCESS_KEY"):
        assert_no_live_secrets('{"cmdline": "s3-key=kdive-demo-secret"}', "report")


def test_a_clean_report_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "kdive-demo-secret")
    assert_no_live_secrets('{"panic": "Oops: 0000 [#1] SMP"}', "report")


def test_the_check_refuses_to_run_with_no_live_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole point: with nothing to look for, passing would be a lie. It must fail loudly.
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("KDIVE_TEST_S3_SECRET_KEY", raising=False)
    with pytest.raises(AssertionError, match="would prove nothing"):
        assert_no_live_secrets("anything at all", "report")


def test_the_secondary_secret_env_is_also_hunted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("KDIVE_TEST_S3_SECRET_KEY", "minio-secret-value")
    with pytest.raises(AssertionError, match="KDIVE_TEST_S3_SECRET_KEY"):
        assert_no_live_secrets("leaked minio-secret-value here", "report")
