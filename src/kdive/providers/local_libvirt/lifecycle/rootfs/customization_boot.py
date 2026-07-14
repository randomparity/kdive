"""Customization-boot console classification (ADR-0345).

The boot-to-self-customize mechanism replaces `virt-customize --install/--run-command`
execution with a transient domain that boots the rootfs's own kernel, runs a firstboot
script, and reports completion via a console marker line. This module owns the shared
constants that both the firstboot renderer (`renderers.py`) and the offline injector
(`rootfs_build.py`) must agree on, plus the console classifier that turns a raw console
capture into an :class:`CustomizeVerdict`.

The genuine-fault pattern is the provisioning boot's `_CRASH_SIGNATURE`
(`kdive.providers.local_libvirt.lifecycle.boot.readiness`) minus the two signatures that are
benign under TCG emulation: a bare `detected stall` (RCU stall warnings are common under slow
TCG execution and are not fatal) and a soft-lockup watchdog's own `BUG:` line (`watchdog: BUG:
soft lockup ...` is a warning, not a kernel fault) — a real fault (e.g. `BUG: unable to handle
kernel paging request`) still matches.
"""

from __future__ import annotations

import re
from enum import StrEnum

OK_MARKER = "kdive-customize-ok"
FAIL_MARKER = "kdive-customize-failed"
CUSTOMIZE_UNIT = "kdive-customize.service"
CUSTOMIZE_SCRIPT_PATH = "/usr/local/sbin/kdive-customize"

_GENUINE_FAULT = re.compile(
    r"Kernel panic"
    r"|(?<![A-Za-z])BUG:(?! soft lockup)"
    r"|(?<![A-Za-z])Oops:"
    r"|general protection fault"
    r"|[Uu]nable to handle kernel"
    r"|KASAN:"
    r"|KFENCE:"
)


class CustomizeVerdict(StrEnum):
    OK = "ok"
    FAILED = "failed"
    PENDING = "pending"


def _line_present(text: str, marker: str) -> bool:
    marker_re = re.compile(rf"^[^\S\n]*{re.escape(marker)}[^\S\n]*$", re.MULTILINE)
    return marker_re.search(text) is not None


def classify_customization_console(data: bytes) -> CustomizeVerdict:
    """Classify a customization-boot console capture as ok, failed, or pending.

    Order matters: the ok marker wins outright; otherwise the fail marker or a genuine
    kernel fault means failed; otherwise the boot is still pending.
    """
    text = data.decode("utf-8", errors="replace")
    if _line_present(text, OK_MARKER):
        return CustomizeVerdict.OK
    if _line_present(text, FAIL_MARKER):
        return CustomizeVerdict.FAILED
    if _GENUINE_FAULT.search(text):
        return CustomizeVerdict.FAILED
    return CustomizeVerdict.PENDING
