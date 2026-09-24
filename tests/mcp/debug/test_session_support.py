"""Unit tests for the host-arch-resolved live debug profile helpers (#2695).

``PROFILE`` stays fixed x86_64 for the unit-test consumers that never touch a real host;
``live_profile``/``require_live_gdbstub_arch`` are the live-only helpers that instead take their
arch from the host, so a native gdbstub debug proof matches what KVM will accept there.
"""

from __future__ import annotations

import pytest

from tests.mcp.debug import session_support


class _FakeUname:
    def __init__(self, machine: str) -> None:
        self.machine = machine


def test_live_profile_resolves_machine_from_arch_traits_for_ppc64le() -> None:
    profile = session_support.live_profile("ppc64le")
    assert profile["arch"] == "ppc64le"
    assert profile["provider"]["local-libvirt"]["domain_xml_params"] == {"machine": "pseries"}


def test_live_profile_keeps_todays_x86_64_machine() -> None:
    profile = session_support.live_profile("x86_64")
    assert profile["arch"] == "x86_64"
    assert profile["provider"]["local-libvirt"]["domain_xml_params"] == {"machine": "q35"}


def test_live_profile_does_not_mutate_the_shared_fixed_profile() -> None:
    session_support.live_profile("ppc64le")
    assert session_support.PROFILE["arch"] == "x86_64"
    assert session_support.PROFILE["provider"]["local-libvirt"]["domain_xml_params"] == {
        "machine": "q35"
    }


def test_require_live_gdbstub_arch_returns_x86_64_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_support.os, "uname", lambda: _FakeUname("x86_64"))
    assert session_support.require_live_gdbstub_arch() == "x86_64"


def test_require_live_gdbstub_arch_skips_with_a_reason_on_ppc64le(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_support.os, "uname", lambda: _FakeUname("ppc64le"))
    with pytest.raises(pytest.skip.Exception, match="ppc64le.*#2736"):
        session_support.require_live_gdbstub_arch()


def test_require_live_gdbstub_arch_skips_rather_than_fails_on_an_unrelated_arch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host arch_traits does not even know (e.g. aarch64) still skips, never raises."""
    monkeypatch.setattr(session_support.os, "uname", lambda: _FakeUname("aarch64"))
    with pytest.raises(pytest.skip.Exception, match="aarch64"):
        session_support.require_live_gdbstub_arch()
