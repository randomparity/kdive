"""Tests for the provision-time overlay-customizer seam (ADR-0289, #963)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.rootfs import (
    overlay_customize as overlay_customize_module,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.overlay_customize import (
    inject_authorized_key_argv,
)


def test_inject_authorized_key_argv_uses_ssh_inject_root() -> None:
    argv = inject_authorized_key_argv("/var/lib/kdive/rootfs/s-overlay.qcow2", "/tmp/k.pub")
    j = " ".join(argv)
    assert argv[0] == "virt-customize"
    assert "-a" in argv and "/var/lib/kdive/rootfs/s-overlay.qcow2" in argv
    assert "--ssh-inject" in argv and "root:file:/tmp/k.pub" in j


def test_real_inject_authorized_key_unresolvable_virt_customize_is_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `virt-customize` resolves against the explicit provider-tool search dirs, never PATH
    # (#2333, mirroring #2300's `_resolve_depmod`); an unresolvable binary fails fast before
    # ever invoking subprocess.run.
    monkeypatch.setattr(overlay_customize_module, "resolve_provider_tool", lambda _tool: None)

    def _must_not_run(*_: object, **__: object) -> None:
        raise AssertionError(
            "subprocess.run must not be reached when virt-customize is unresolvable"
        )

    monkeypatch.setattr(overlay_customize_module.subprocess, "run", _must_not_run)

    with pytest.raises(CategorizedError) as caught:
        overlay_customize_module._real_inject_authorized_key("/overlay.qcow2", "ssh-ed25519 AAAA")

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert str(caught.value) == (
        "virt-customize is not installed; cannot inject the per-System bootstrap key; not found "
        f"in any of {overlay_customize_module.PROVIDER_TOOL_SEARCH_PATH}"
    )
    assert caught.value.details == {"searched": overlay_customize_module.PROVIDER_TOOL_SEARCH_PATH}
