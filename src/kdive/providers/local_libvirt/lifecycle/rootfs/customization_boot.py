"""Customization-boot console classification, orchestration, and offline seal (ADR-0345).

The boot-to-self-customize mechanism replaces `virt-customize --install/--run-command`
execution with a transient domain that boots the rootfs's own kernel, runs a firstboot
script, and reports completion via a console marker line. This module owns the shared
constants that both the firstboot renderer (`renderers.py`) and the offline injector
(`rootfs_build.py`) must agree on, the console classifier that turns a raw console capture
into a :class:`CustomizeVerdict`, the `run_customization_boot` orchestration that drives the
transient domain to completion, and `seal_customized_image`, which offline-reseals the image
(cloud-init reset, optional SELinux relabel, firstboot-unit-removed assertion) once the boot
reports ok.

The genuine-fault pattern is the provisioning boot's `_CRASH_SIGNATURE`
(`kdive.providers.local_libvirt.lifecycle.boot.readiness`) minus the two signatures that are
benign under TCG emulation: a bare `detected stall` (RCU stall warnings are common under slow
TCG execution and are not fatal) and a soft-lockup watchdog's own `BUG:` line (`watchdog: BUG:
soft lockup ...` is a warning, not a kernel fault) — a real fault (e.g. `BUG: unable to handle
kernel paging request`) still matches.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from math import ceil
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes._build_common import run_guestfs_tool
from kdive.providers.local_libvirt.lifecycle.boot.readiness import _domain_exit_probe
from kdive.providers.local_libvirt.lifecycle.deadlines import tcg_deadline_multiplier
from kdive.providers.local_libvirt.lifecycle.storage import _prepare_console_log
from kdive.providers.local_libvirt.settings import (
    LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S,
    LIBVIRT_URI,
)
from kdive.providers.shared.build_timeouts import SLOW_BUILD_TOOL_TIMEOUT_S
from kdive.providers.shared.runtime_paths import (
    build_domain_name,
    console_log_path,
    read_console_log,
)

_log = logging.getLogger(__name__)

OK_MARKER = "kdive-customize-ok"
FAIL_MARKER = "kdive-customize-failed"
CUSTOMIZE_UNIT = "kdive-customize.service"
CUSTOMIZE_SCRIPT_PATH = "/usr/local/sbin/kdive-customize"

# Genuine kernel-fatal patterns, used ONLY as a backstop for a guest that dies without emitting
# the (authoritative) fail marker — e.g. a panic that kills the shell before the firstboot script
# writes its verdict. This scan runs over the WHOLE customization console, which (unlike a quiet
# provision boot)
# carries `dnf` transaction + scriptlet output, so the broad `BUG:` alternative is deliberately
# EXCLUDED: a package changelog or scriptlet line containing `BUG:` would false-fail an otherwise
# good build. The retained patterns are kernel-log-specific and do not appear in package output.
# The common panic-then-die case is already caught by settled-without-ok; this only speeds a
# hung panic. `soft lockup`/`detected stall` stay excluded (benign under TCG load) (ADR-0345).
_GENUINE_FAULT = re.compile(
    r"Kernel panic"
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


# The customization boot is minutes-to-tens-of-minutes (package install + initramfs rebuild), so
# the completion poll is coarser than the boot-readiness 5.0s cadence; the window budget scales
# with the accelerator's TCG multiplier (ADR-0341).
_POLL_INTERVAL_S = 10.0
_CONSOLE_TAIL_CHARS = 800


class _Domain(Protocol):
    """The libvirt transient-domain surface the teardown drives."""

    def isActive(self) -> int: ...  # noqa: N802 - mirrors the libvirt binding name
    def destroy(self) -> int: ...


class _Conn(Protocol):
    """The libvirt connection surface the orchestration holds open for the whole call."""

    def createXML(  # noqa: N802, N803 - mirrors the libvirt binding names
        self, xmlDesc: str, flags: int
    ) -> _Domain: ...
    def close(self) -> int: ...


@dataclass(frozen=True, slots=True)
class CustomizationBootSeams:
    """Injected host seams for one customization boot (ADR-0345).

    Every real-host interaction (libvirt connect/createXML, console-log read, domstate probe,
    sleep, poll-budget) is a seam so the orchestration is exercised entirely in-process without
    libguestfs/qemu/network. :meth:`from_env` wires the live implementations.
    """

    prepare_console: Callable[[UUID], None]
    open_conn: Callable[[], _Conn]
    create_transient: Callable[[_Conn, str], _Domain]
    read_console: Callable[[UUID], bytes]
    domain_settled: Callable[[UUID], bool]
    sleep: Callable[[float], None]
    window_polls: Callable[[str], int]

    @classmethod
    def from_env(cls) -> CustomizationBootSeams:  # pragma: no cover - live_vm
        """Wire the live host seams from configuration (ADR-0345/0341/0223).

        The connection is opened once and returned to the caller, which holds it open for the
        whole boot: closing it triggers ``VIR_DOMAIN_START_AUTODESTROY`` cleanup, so an
        opened/closed-per-poll connection would reap the domain mid-customization.
        ``prepare_console`` creates the console-log directory and worker-owned ``0644`` file
        *before* the domain starts (``storage._prepare_console_log``), guaranteeing
        ``/var/lib/kdive/console`` exists on a never-provisioned build host. The completion
        handshake reads that serial ``<log>``, which virtlogd writes ``root:0600``; whether the
        pre-touched worker-owned file survives (so a **non-root** reader can read it) is
        virtlogd-version-dependent — on libvirt 12 virtlogd unlinks+recreates it ``root:0600`` and
        the truncate-in-place mitigation does not hold, so the reliable readers are the worker
        running as **root** (the deployment default) or ``KDIVE_LIBVIRT_URI=qemu:///session``
        (session virtlogd writes the log worker-owned) — see the #1147 proof record. The settled
        probe is the crashed-aware domstate probe (shut off *or* crashed).
        """
        uri = config.require(LIBVIRT_URI)
        return cls(
            prepare_console=lambda bid: _prepare_console_log(console_log_path(bid)),
            open_conn=lambda: libvirt.open(uri),
            create_transient=lambda conn, xml: conn.createXML(
                xml, libvirt.VIR_DOMAIN_START_AUTODESTROY
            ),
            read_console=lambda bid: read_console_log(console_log_path(bid)),
            domain_settled=lambda bid: _domain_exit_probe(build_domain_name(bid)).exited,
            sleep=time.sleep,
            window_polls=_real_window_polls,
        )


def _real_window_polls(accel: str) -> int:  # pragma: no cover - live_vm
    """Poll budget = base window / interval, scaled by the accelerator's TCG factor (ADR-0341)."""
    base = config.require(LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S)
    return ceil(base / _POLL_INTERVAL_S * tcg_deadline_multiplier(accel))


def run_customization_boot(
    build_id: UUID, domain_xml: str, *, accel: str, seams: CustomizationBootSeams
) -> None:
    """Boot a transient domain that self-customizes and wait for its completion marker (ADR-0345).

    Opens exactly one libvirt connection and creates the transient AUTODESTROY domain on it, then
    polls the console for the ``kdive-customize-ok``/``-failed`` marker (or a genuine kernel
    fault) within the accelerator-scaled window. The connection stays open for the whole call —
    it is force-off + closed only in the ``finally`` (closing triggers AUTODESTROY cleanup).

    Args:
        build_id: The build whose transient domain and console log this boot owns.
        domain_xml: The rendered customization-boot domain XML.
        accel: The resolved accelerator (``"kvm"``/``"tcg"``) scaling the poll window.
        seams: The injected host seams (see :class:`CustomizationBootSeams`).

    Raises:
        CategorizedError: ``PROVISIONING_FAILURE`` (with ``details["console_tail"]``) on a
            fail-marker, genuine-fault, or domain-settled-without-ok verdict; ``BOOT_TIMEOUT``
            (also with the tail) when the window is exhausted; and it lets a console-read
            ``CONFIGURATION_ERROR`` (the ADR-0223 readability wall) propagate.
    """
    seams.prepare_console(build_id)
    conn = seams.open_conn()
    domain: _Domain | None = None
    try:
        domain = seams.create_transient(conn, domain_xml)
        _await_customize_ok(build_id, seams, seams.window_polls(accel))
    finally:
        _teardown(domain, conn)


def _await_customize_ok(build_id: UUID, seams: CustomizationBootSeams, polls: int) -> None:
    """Poll the console up to ``polls`` times; return on ok, raise on failure/timeout."""
    last_data = b""
    for _ in range(polls):
        last_data = seams.read_console(build_id)
        verdict = classify_customization_console(last_data)
        if verdict is CustomizeVerdict.OK:
            return
        if verdict is CustomizeVerdict.FAILED:
            raise _provisioning_failure("customization boot reported failure", last_data)
        if seams.domain_settled(build_id):
            _raise_unless_settled_ok(build_id, seams)
            return
        seams.sleep(_POLL_INTERVAL_S)
    raise _boot_timeout(last_data)


def _raise_unless_settled_ok(build_id: UUID, seams: CustomizationBootSeams) -> None:
    """Re-read a settled domain's console: return on ok, else raise settled-without-ok."""
    data = seams.read_console(build_id)
    if classify_customization_console(data) is CustomizeVerdict.OK:
        return
    raise _provisioning_failure("customization boot domain settled without the ok marker", data)


def _teardown(domain: _Domain | None, conn: _Conn) -> None:
    """Force the domain off (if active) and only then close the connection (AUTODESTROY)."""
    if domain is not None:
        _force_off(domain)
    _close(conn)


def _force_off(domain: _Domain) -> None:
    """Destroy the transient domain if it is still running (best-effort).

    The firstboot's own ``systemctl poweroff`` is the success path, so the transient domain has
    usually already vanished by the time teardown runs — ``isActive``/``destroy`` then raise
    ``VIR_ERR_NO_DOMAIN``, which is the expected benign end state, not a failure. Only a genuine
    force-off error is worth a warning; otherwise every successful build would log a traceback.
    """
    try:
        if domain.isActive():
            domain.destroy()
    except libvirt.libvirtError as err:  # pragma: no cover - live_vm
        if err.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            return
        _log.warning("force-off of the customization-boot domain failed; continuing", exc_info=True)


def _close(conn: _Conn) -> None:
    """Close the libvirt connection, swallowing a close-time error (best-effort cleanup)."""
    try:
        conn.close()
    except libvirt.libvirtError:  # pragma: no cover - live_vm
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


def _console_tail(data: bytes) -> str:
    """Decode the console bytes (utf-8, replacement) and keep the last ~800 chars.

    A build has no ``RequestContext``/secret registry, so this is a plain bounded tail rather
    than :func:`redacted_console_tail` — there is nothing to redact against here.
    """
    return data.decode("utf-8", errors="replace")[-_CONSOLE_TAIL_CHARS:]


def _provisioning_failure(message: str, data: bytes) -> CategorizedError:
    return CategorizedError(
        message,
        category=ErrorCategory.PROVISIONING_FAILURE,
        details={"console_tail": _console_tail(data)},
    )


def _boot_timeout(data: bytes) -> CategorizedError:
    return CategorizedError(
        "customization boot did not reach the ok marker within the window",
        category=ErrorCategory.BOOT_TIMEOUT,
        details={"console_tail": _console_tail(data)},
    )


type GuestfishRunner = Callable[[Path, str], str]

_SEAL_UNIT_NOT_REMOVED_MESSAGE = (
    "customization firstboot unit was not self-removed; the build boot did not complete cleanly"
)
_SEAL_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S


def _seal_script(*, unit_name: str, selinux: bool) -> str:
    """Render the offline seal guestfish script (ADR-0345).

    Resets cloud-init's per-instance state (so a subsequent provision boot re-runs
    ``resize_rootfs`` against the fresh overlay rather than a stale seen-once record),
    optionally forces a first-boot SELinux relabel, and — as its last two lines — emits the
    unit-removed evidence: the libguestfs-**native** ``is-file``/``is-symlink`` predicates print
    ``true``/``false`` for the firstboot unit file and its ``multi-user.target.wants`` symlink,
    which :func:`seal_customized_image` parses. These are appliance operations on guest *data*, so
    they work on a foreign-arch image; a guest-command ``sh 'test ...'`` would instead exec the
    guest's ``/bin/sh`` in the host-arch appliance and fail ``Exec format error`` cross-arch — the
    very limitation this whole build path exists to avoid (ADR-0345).
    """
    lines = [
        "rm-rf /var/lib/cloud/instances",
        "rm-rf /var/lib/cloud/instance",
        "rm-rf /var/lib/cloud/sem",
        "rm-rf /var/lib/cloud/data",
    ]
    if selinux:
        lines.append("touch /.autorelabel")
    lines.append(f"is-file /etc/systemd/system/{unit_name}")
    lines.append(f"is-symlink /etc/systemd/system/multi-user.target.wants/{unit_name}")
    return "\n".join(lines) + "\n"


def seal_customized_image(
    qcow2: Path, *, unit_name: str, selinux: bool, run_guestfish: GuestfishRunner
) -> None:
    """Offline-seal a customized rootfs image after a successful customization boot (ADR-0345).

    Runs one guestfish script (via the injected ``run_guestfish``) that resets cloud-init's
    per-instance state, optionally touches ``/.autorelabel``, and prints the unit file's and
    wants-symlink's presence. A ``true`` in that output means the firstboot did not self-remove
    (the build boot did not complete cleanly), which this raises as ``PROVISIONING_FAILURE`` —
    an arch-safe replacement for the former ``sh 'test'`` guest-command check that broke on a
    foreign-arch image.

    Args:
        qcow2: The staged rootfs image the customization boot ran against.
        unit_name: The firstboot unit name that must have self-removed (``CUSTOMIZE_UNIT``).
        selinux: Whether to force a first-boot SELinux relabel via ``/.autorelabel``.
        run_guestfish: The injected guestfish script runner (real or fake), returning stdout.
    """
    output = run_guestfish(qcow2, _seal_script(unit_name=unit_name, selinux=selinux))
    if "true" in output.split():
        raise CategorizedError(
            _SEAL_UNIT_NOT_REMOVED_MESSAGE,
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"unit": unit_name},
        )


def _real_run_guestfish(qcow2: Path, script: str) -> str:  # pragma: no cover - live_vm
    """Run a guestfish script against ``qcow2``, returning its stdout (mapping failure)."""
    return run_guestfs_tool(
        ["guestfish", "--rw", "-a", str(qcow2), "-i"],
        stage="customization-seal",
        timeout_s=_SEAL_TIMEOUT_S,
        missing_message="guestfish is not installed; cannot seal the customized rootfs image",
        input_text=script,
    )
