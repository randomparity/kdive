"""Unit tests for provider-aware platform cmdline composition (ADR-0183, #587)."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.services.runs.steps import platform_owned_cmdline_token, system_required_cmdline

_LOCAL_ROOT = "root=/dev/vda"


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
