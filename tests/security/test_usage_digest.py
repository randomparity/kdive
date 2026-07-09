"""`digest_args` — stable, order-independent, secret-free arg digest (#1010, ADR-0304)."""

from __future__ import annotations

from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.usage import digest_args


def _redactor(*secrets: str) -> Redactor:
    registry = SecretRegistry()
    for value in secrets:
        registry.register(value, scope=None)
    return Redactor(registry=registry)


def test_stable_for_identical_args() -> None:
    redactor = _redactor()
    assert digest_args(redactor, {"a": 1, "b": "x"}) == digest_args(redactor, {"a": 1, "b": "x"})


def test_key_order_does_not_change_digest() -> None:
    redactor = _redactor()
    assert digest_args(redactor, {"a": 1, "b": 2}) == digest_args(redactor, {"b": 2, "a": 1})


def test_none_and_empty_map_agree_and_are_non_empty() -> None:
    redactor = _redactor()
    digest = digest_args(redactor, None)
    assert digest == digest_args(redactor, {})
    assert len(digest) == 64  # sha256 hex


def test_registered_secret_value_does_not_affect_digest() -> None:
    # Two calls differing only in a registered secret redact to the same structure, so the
    # digest is identical and the secret never reaches the hash.
    redactor = _redactor("s3cr3t-token-value")
    with_secret = digest_args(redactor, {"token": "s3cr3t-token-value", "n": 1})
    redacted = digest_args(redactor, {"token": "[REDACTED]", "n": 1})
    assert with_secret == redacted


def test_secret_keyed_field_is_redacted_regardless_of_value() -> None:
    # A key matching the secret-key pattern is masked by value, so different secrets under
    # a `password` key collapse to the same digest.
    redactor = _redactor()
    one = digest_args(redactor, {"password": "hunter2"})
    two = digest_args(redactor, {"password": "correcthorse"})
    assert one == two


def test_different_non_secret_args_differ() -> None:
    redactor = _redactor()
    assert digest_args(redactor, {"tool": "runs.install"}) != digest_args(
        redactor, {"tool": "runs.boot"}
    )
