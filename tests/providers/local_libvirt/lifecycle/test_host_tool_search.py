"""Tests for the shared provider-tool resolution helper (#2333, mirroring #2300)."""

from __future__ import annotations

import pytest

from kdive.providers.local_libvirt.lifecycle import host_tool_search


def test_resolve_provider_tool_searches_only_the_fixed_dirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_which(name: str, path: str | None = None) -> str | None:
        captured["which"] = (name, path)
        return "/usr/bin/virsh"

    monkeypatch.setattr(host_tool_search.shutil, "which", fake_which)

    resolved = host_tool_search.resolve_provider_tool("virsh")

    assert resolved == "/usr/bin/virsh"
    assert captured["which"] == ("virsh", "/usr/bin:/bin")


def test_resolve_provider_tool_returns_none_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_tool_search.shutil, "which", lambda _name, path=None: None)

    assert host_tool_search.resolve_provider_tool("qemu-img") is None


def test_provider_tool_search_path_is_the_joined_search_dirs() -> None:
    assert host_tool_search.PROVIDER_TOOL_SEARCH_DIRS == ("/usr/bin", "/bin")
    assert host_tool_search.PROVIDER_TOOL_SEARCH_PATH == "/usr/bin:/bin"
