"""The shared live-script output assembly: redact platform secrets, then byte-cap (ADR-0240)."""

from __future__ import annotations

from kdive.providers.ports.retrieve import LiveScriptOutput
from kdive.providers.shared.debug_common.introspect import assemble_script_output
from kdive.security.secrets.secret_registry import SecretRegistry


def test_script_output_redacts_registered_secret() -> None:
    reg = SecretRegistry()
    reg.register("TOPSECRET", scope=None)
    out = assemble_script_output("value=TOPSECRET\n", byte_cap=1024, secret_registry=reg)
    assert "TOPSECRET" not in out.output
    assert out.truncated is False


def test_script_output_byte_caps_and_flags_truncated() -> None:
    out = assemble_script_output("x" * 100, byte_cap=10, secret_registry=SecretRegistry())
    assert len(out.output.encode("utf-8")) <= 10
    assert out.truncated is True
    assert isinstance(out, LiveScriptOutput)


def test_script_output_under_cap_is_not_truncated() -> None:
    out = assemble_script_output("hello\n", byte_cap=1024, secret_registry=SecretRegistry())
    assert out.output == "hello\n"
    assert out.truncated is False
