"""Unit tests for provider-aware platform cmdline composition (ADR-0183, #587)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.lifecycle.records import Run
from kdive.services.runs import steps as steps_mod
from kdive.services.runs.steps import (
    BuildStepResult,
    cmdline_for,
    platform_owned_cmdline_token,
    system_required_cmdline,
)

_LOCAL_ROOT = "root=/dev/vda"


def _fake_run() -> Run:
    """A stand-in Run — only ``.id`` is read, and only on the no-override branch."""
    return cast(Run, SimpleNamespace(id=uuid4()))


def test_local_root_kdump_keeps_root_and_crashkernel() -> None:
    assert (
        system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT)
        == "console=ttyS0 root=/dev/vda crashkernel=256M"
    )


def test_remote_none_root_kdump_omits_root_keeps_crashkernel() -> None:
    # Remote-libvirt owns no platform root= (the in-guest GRUB supplies root=UUID via copy-default).
    assert system_required_cmdline(CaptureMethod.KDUMP, None) == "console=ttyS0 crashkernel=256M"


def test_remote_none_root_console_is_console_only() -> None:
    assert system_required_cmdline(CaptureMethod.CONSOLE, None) == "console=ttyS0"


def test_empty_root_is_treated_as_no_root_not_a_stray_token() -> None:
    # An empty root device means the platform injects none; it must not leave a stray empty token.
    assert system_required_cmdline(CaptureMethod.CONSOLE, "") == "console=ttyS0"
    assert system_required_cmdline(CaptureMethod.KDUMP, "") == "console=ttyS0 crashkernel=256M"


def test_local_root_console_omits_crashkernel() -> None:
    assert (
        system_required_cmdline(CaptureMethod.CONSOLE, _LOCAL_ROOT) == "console=ttyS0 root=/dev/vda"
    )


def test_gdbstub_appends_nokaslr_so_vmlinux_symbols_match_running_base() -> None:
    # A gdbstub-debug System boots with -gdb; KASLR (CONFIG_RANDOMIZE_BASE=y) would relocate the
    # running kernel away from the fetched vmlinux's link base, so breakpoints set by symbol
    # never fire (#711). nokaslr pins the running base to the symbol addresses.
    assert (
        system_required_cmdline(CaptureMethod.GDBSTUB, _LOCAL_ROOT)
        == "console=ttyS0 root=/dev/vda nokaslr"
    )


def test_non_gdbstub_boots_never_carry_nokaslr() -> None:
    # nokaslr is debug-only: a normal console/kdump boot keeps KASLR enabled.
    assert "nokaslr" not in system_required_cmdline(CaptureMethod.CONSOLE, _LOCAL_ROOT)
    assert "nokaslr" not in system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT)
    assert "nokaslr" not in system_required_cmdline(CaptureMethod.HOST_DUMP, _LOCAL_ROOT)


def test_console_is_always_first_then_root_then_crashkernel() -> None:
    # Deterministic token order regardless of method/root.
    assert system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT).split() == [
        "console=ttyS0",
        "root=/dev/vda",
        "crashkernel=256M",
    ]


def test_platform_owned_tokens_still_reject_root_on_any_provider() -> None:
    # Admission set is unchanged: a user build cmdline may never set root=.
    assert platform_owned_cmdline_token("root=/dev/sda1 quiet") == "root="
    assert platform_owned_cmdline_token("console=ttyS0") == "console="
    assert platform_owned_cmdline_token("dhash_entries=1") is None


def test_kdump_crashkernel_override_replaces_default_size() -> None:
    # A per-install crashkernel reservation (ADR-0300, #989) replaces the default 256M.
    assert (
        system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT, crashkernel="512M")
        == "console=ttyS0 root=/dev/vda crashkernel=512M"
    )


def test_kdump_crashkernel_accepts_a_range_token() -> None:
    # The token is opaque (the booted kernel is the grammar arbiter): a multi-range value rides.
    assert (
        system_required_cmdline(CaptureMethod.KDUMP, None, crashkernel="1G-2G:128M,2G-:256M")
        == "console=ttyS0 crashkernel=1G-2G:128M,2G-:256M"
    )


def test_kdump_crashkernel_none_falls_back_to_default() -> None:
    assert (
        system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT, crashkernel=None)
        == "console=ttyS0 root=/dev/vda crashkernel=256M"
    )


def test_non_kdump_ignores_crashkernel_override() -> None:
    # crashkernel is a kdump-only token: a non-kdump method never emits it, even when supplied.
    assert (
        system_required_cmdline(CaptureMethod.CONSOLE, _LOCAL_ROOT, crashkernel="512M")
        == "console=ttyS0 root=/dev/vda"
    )
    assert "crashkernel" not in system_required_cmdline(
        CaptureMethod.GDBSTUB, _LOCAL_ROOT, crashkernel="512M"
    )


def test_cmdline_for_threads_crashkernel_orthogonal_to_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # crashkernel and the ADR-0299 cmdline override are independent: both apply, platform-first.
    async def _must_not_read(conn: object, run_id: object) -> BuildStepResult | None:
        raise AssertionError("override path must not read the build ledger")

    monkeypatch.setattr(steps_mod, "existing_build_result", _must_not_read)

    async def _run() -> str:
        return await cmdline_for(
            cast("steps_mod.AsyncConnection", None),
            _fake_run(),
            CaptureMethod.KDUMP,
            root_cmdline=_LOCAL_ROOT,
            override="dhash_entries=1",
            crashkernel="512M",
        )

    assert asyncio.run(_run()) == "console=ttyS0 root=/dev/vda crashkernel=512M dhash_entries=1"


def test_cmdline_for_override_replaces_build_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    # The install override (ADR-0299) replaces the build-baked extra without reading the ledger.
    async def _must_not_read(conn: object, run_id: object) -> BuildStepResult | None:
        raise AssertionError("override path must not read the build ledger")

    monkeypatch.setattr(steps_mod, "existing_build_result", _must_not_read)

    async def _run() -> str:
        return await cmdline_for(
            cast("steps_mod.AsyncConnection", None),
            _fake_run(),
            CaptureMethod.HOST_DUMP,
            root_cmdline=_LOCAL_ROOT,
            override="  dhash_entries=1 ",
        )

    assert asyncio.run(_run()) == "console=ttyS0 root=/dev/vda dhash_entries=1"


def test_cmdline_for_no_override_appends_build_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _build_result(conn: object, run_id: object) -> BuildStepResult | None:
        return BuildStepResult(
            kernel_ref=None, debuginfo_ref=None, build_id=None, cmdline="dhash_entries=9"
        )

    monkeypatch.setattr(steps_mod, "existing_build_result", _build_result)

    async def _run() -> str:
        return await cmdline_for(
            cast("steps_mod.AsyncConnection", None),
            _fake_run(),
            CaptureMethod.HOST_DUMP,
            root_cmdline=_LOCAL_ROOT,
        )

    assert asyncio.run(_run()) == "console=ttyS0 root=/dev/vda dhash_entries=9"
