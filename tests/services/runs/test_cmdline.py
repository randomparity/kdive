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
_X86 = "x86_64"


def _fake_run() -> Run:
    """A stand-in Run — only ``.id`` is read, and only on the no-override branch."""
    return cast(Run, SimpleNamespace(id=uuid4()))


def test_local_root_kdump_keeps_root_and_crashkernel() -> None:
    assert (
        system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT, arch=_X86)
        == "console=ttyS0 root=/dev/vda crashkernel=256M"
    )


def test_remote_none_root_kdump_omits_root_keeps_crashkernel() -> None:
    # Remote-libvirt owns no platform root= (the in-guest GRUB supplies root=UUID via copy-default).
    assert (
        system_required_cmdline(CaptureMethod.KDUMP, None, arch=_X86)
        == "console=ttyS0 crashkernel=256M"
    )


def test_remote_none_root_console_is_console_only() -> None:
    assert system_required_cmdline(CaptureMethod.CONSOLE, None, arch=_X86) == "console=ttyS0"


def test_empty_root_is_treated_as_no_root_not_a_stray_token() -> None:
    # An empty root device means the platform injects none; it must not leave a stray empty token.
    assert system_required_cmdline(CaptureMethod.CONSOLE, "", arch=_X86) == "console=ttyS0"
    assert (
        system_required_cmdline(CaptureMethod.KDUMP, "", arch=_X86)
        == "console=ttyS0 crashkernel=256M"
    )


def test_local_root_console_omits_crashkernel() -> None:
    assert (
        system_required_cmdline(CaptureMethod.CONSOLE, _LOCAL_ROOT, arch=_X86)
        == "console=ttyS0 root=/dev/vda"
    )


def test_ppc64le_leads_with_the_hvc0_console_and_512m_default() -> None:
    # pseries has no ttyS0; the serial console is hvc0 (spapr-vty). A ppc64le System must lead with
    # console=hvc0 or the readiness marker never reaches the host serial log and boot times out.
    # The crashkernel default is the per-arch value from arch_traits — 512M on ppc64le, not the
    # x86 256M (#1148, ADR-0346).
    assert (
        system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT, arch="ppc64le")
        == "console=hvc0 root=/dev/vda crashkernel=512M"
    )


def test_ppc64le_explicit_crashkernel_still_wins_over_the_arch_default() -> None:
    # The ADR-0300 per-install reservation overrides the per-arch default on ppc64le too — only the
    # None fallback is arch-keyed.
    assert (
        system_required_cmdline(
            CaptureMethod.KDUMP, _LOCAL_ROOT, arch="ppc64le", crashkernel="384M"
        )
        == "console=hvc0 root=/dev/vda crashkernel=384M"
    )


def test_fadump_appends_fadump_on_after_the_reservation() -> None:
    # fadump reuses the crashkernel reservation and adds fadump=on (last), on ppc64le (ADR-0349).
    assert (
        system_required_cmdline(CaptureMethod.FADUMP, _LOCAL_ROOT, arch="ppc64le")
        == "console=hvc0 root=/dev/vda crashkernel=512M fadump=on"
    )


def test_fadump_explicit_crashkernel_still_wins_then_fadump_on() -> None:
    # An explicit reservation overrides the arch default on the fadump path too; fadump=on is last.
    assert (
        system_required_cmdline(CaptureMethod.FADUMP, _LOCAL_ROOT, arch="ppc64le", crashkernel="1G")
        == "console=hvc0 root=/dev/vda crashkernel=1G fadump=on"
    )


def test_non_fadump_boots_never_carry_fadump_on() -> None:
    # fadump=on is fadump-only: kdump and other methods never emit it (regression guard).
    assert "fadump" not in system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT, arch="ppc64le")
    assert "fadump" not in system_required_cmdline(CaptureMethod.CONSOLE, _LOCAL_ROOT, arch=_X86)


def test_fadump_is_a_platform_owned_cmdline_token() -> None:
    # A caller install-cmdline override cannot inject a conflicting fadump= token (ADR-0349 §3).
    assert platform_owned_cmdline_token("fadump=off other=1") == "fadump="


def test_gdbstub_appends_nokaslr_so_vmlinux_symbols_match_running_base() -> None:
    # A gdbstub-debug System boots with -gdb; KASLR (CONFIG_RANDOMIZE_BASE=y) would relocate the
    # running kernel away from the fetched vmlinux's link base, so breakpoints set by symbol
    # never fire (#711). nokaslr pins the running base to the symbol addresses.
    assert (
        system_required_cmdline(CaptureMethod.GDBSTUB, _LOCAL_ROOT, arch=_X86)
        == "console=ttyS0 root=/dev/vda nokaslr"
    )


def test_non_gdbstub_boots_never_carry_nokaslr() -> None:
    # nokaslr is debug-only: a normal console/kdump boot keeps KASLR enabled.
    assert "nokaslr" not in system_required_cmdline(CaptureMethod.CONSOLE, _LOCAL_ROOT, arch=_X86)
    assert "nokaslr" not in system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT, arch=_X86)
    assert "nokaslr" not in system_required_cmdline(CaptureMethod.HOST_DUMP, _LOCAL_ROOT, arch=_X86)


def test_console_is_always_first_then_root_then_crashkernel() -> None:
    # Deterministic token order regardless of method/root.
    assert system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT, arch=_X86).split() == [
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
        system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT, arch=_X86, crashkernel="512M")
        == "console=ttyS0 root=/dev/vda crashkernel=512M"
    )


def test_kdump_crashkernel_accepts_a_range_token() -> None:
    # The token is opaque (the booted kernel is the grammar arbiter): a multi-range value rides.
    assert (
        system_required_cmdline(
            CaptureMethod.KDUMP, None, arch=_X86, crashkernel="1G-2G:128M,2G-:256M"
        )
        == "console=ttyS0 crashkernel=1G-2G:128M,2G-:256M"
    )


def test_kdump_crashkernel_none_falls_back_to_default() -> None:
    assert (
        system_required_cmdline(CaptureMethod.KDUMP, _LOCAL_ROOT, arch=_X86, crashkernel=None)
        == "console=ttyS0 root=/dev/vda crashkernel=256M"
    )


def test_non_kdump_ignores_crashkernel_override() -> None:
    # crashkernel is a kdump-only token: a non-kdump method never emits it, even when supplied.
    assert (
        system_required_cmdline(CaptureMethod.CONSOLE, _LOCAL_ROOT, arch=_X86, crashkernel="512M")
        == "console=ttyS0 root=/dev/vda"
    )
    assert "crashkernel" not in system_required_cmdline(
        CaptureMethod.GDBSTUB, _LOCAL_ROOT, arch=_X86, crashkernel="512M"
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
            arch=_X86,
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
            arch=_X86,
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
            arch=_X86,
        )

    assert asyncio.run(_run()) == "console=ttyS0 root=/dev/vda dhash_entries=9"
