"""Tests for value/pattern redaction and the logging filter (ADR-0027)."""

from __future__ import annotations

import logging

from kdive.security.secrets.redaction import (
    REDACTION,
    Redactor,
    SecretRedactionFilter,
    redact_url_credentials,
)
from kdive.security.secrets.secret_registry import SecretRegistry


def test_redact_url_credentials_strips_userinfo() -> None:
    url = "https://user:pass@host/path"  # pragma: allowlist secret
    assert redact_url_credentials(url) == "https://host/path"


def test_redact_url_credentials_strips_schemeless_userinfo() -> None:
    assert redact_url_credentials("user:pass@host/path") == "host/path"


def test_redact_url_credentials_preserves_clean_url_with_colon_at_in_path() -> None:
    url = "https://host/a:b@c"
    assert redact_url_credentials(url) == url


def test_redact_url_credentials_preserves_port() -> None:
    assert redact_url_credentials("https://u:p@host:5432/db") == "https://host:5432/db"


def test_redact_url_credentials_strips_username_only_userinfo() -> None:
    # userinfo can be a bare username with no password; it must still be stripped.
    assert redact_url_credentials("https://user@host/path") == "https://host/path"


def test_redact_url_credentials_handles_userinfo_with_empty_host() -> None:
    # An authority that is all userinfo (no hostname) keeps only the port, never the credentials.
    assert redact_url_credentials("https://user:pass@:5432/db") == "https://:5432/db"


def test_redactor_masks_value_with_regex_metacharacters() -> None:
    redactor = Redactor(["a.b*c+(d)"], registry=SecretRegistry())
    assert redactor.redact_text("prefix a.b*c+(d) suffix") == f"prefix {REDACTION} suffix"


def test_redactor_masks_key_value_pairs() -> None:
    redactor = Redactor(registry=SecretRegistry())
    assert REDACTION in redactor.redact_text("password=hunter2")
    assert REDACTION in redactor.redact_text("token: abc123")


def test_redactor_keeps_key_and_separator_but_replaces_only_the_value() -> None:
    # The substitution preserves the key (group 1) and separator (group 2) and masks the
    # value (group 3); the original value must not survive anywhere in the output.
    redactor = Redactor(registry=SecretRegistry())
    assert redactor.redact_text("password=hunter2") == f"password={REDACTION}"
    assert redactor.redact_text("token: abc123") == f"token: {REDACTION}"
    assert "hunter2" not in redactor.redact_text("password=hunter2")


def test_redactor_recurses_into_nested_structures() -> None:
    redactor = Redactor(["sekret"], registry=SecretRegistry())
    result = redactor.redact_value({"outer": ["sekret", ("sekret",)]})
    assert result == {"outer": [REDACTION, (REDACTION,)]}


def test_redactor_masks_sensitive_path_mapping() -> None:
    redactor = Redactor(registry=SecretRegistry())
    result = redactor.redact_value({"sensitive": True, "path": "/secret/key"})
    assert result["path"] == REDACTION


def test_redactor_keeps_a_path_when_the_mapping_is_not_sensitive() -> None:
    # "path" is masked only when the mapping is explicitly sensitive; a plain path is preserved.
    redactor = Redactor(registry=SecretRegistry())
    result = redactor.redact_value({"path": "/etc/config"})
    assert result == {"path": "/etc/config"}


def test_redactor_masks_a_secret_named_mapping_key() -> None:
    # A key whose name matches the secret-key pattern is masked regardless of sensitivity.
    redactor = Redactor(registry=SecretRegistry())
    result = redactor.redact_value({"token": "abc123", "api_key": "k", "plain": "ok"})
    assert result == {"token": REDACTION, "api_key": REDACTION, "plain": "ok"}


def test_redactor_seeds_only_from_the_explicit_registry() -> None:
    source = SecretRegistry()
    other = SecretRegistry()
    source.register("explicit-registry-sentinel", scope=object())
    other.register("other-registry-sentinel", scope=object())

    redactor = Redactor(registry=source)

    assert redactor.redact_text("leak explicit-registry-sentinel here") == (
        f"leak {REDACTION} here"
    )
    assert (
        redactor.redact_text("leak other-registry-sentinel here")
        == "leak other-registry-sentinel here"
    )


def test_redaction_filter_masks_newly_registered_value() -> None:
    registry = SecretRegistry()
    log_filter = SecretRedactionFilter(registry)
    registry.register("filter-secret", scope=None)
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="saw filter-secret",
        args=(),
        exc_info=None,
    )
    # The filter redacts in place and must keep the record (return True) so it still emits.
    assert log_filter.filter(record) is True
    assert "filter-secret" not in record.getMessage()
    assert REDACTION in record.getMessage()


def test_redaction_filter_rebuilds_only_on_version_change() -> None:
    registry = SecretRegistry()
    log_filter = SecretRedactionFilter(registry)

    def _emit(msg: str) -> str:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        log_filter.filter(record)
        return record.getMessage()

    assert _emit("before any-secret") == "before any-secret"
    registry.register("any-secret", scope=None)
    assert REDACTION in _emit("now any-secret leaks")
