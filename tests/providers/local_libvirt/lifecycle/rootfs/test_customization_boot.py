"""Tests for the customization-boot console classifier + orchestration (ADR-0345)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.rootfs.customization_boot import (
    CUSTOMIZE_UNIT,
    CustomizationBootSeams,
    CustomizeVerdict,
    run_customization_boot,
    seal_customized_image,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.customization_boot import (
    classify_customization_console as C,
)

BID = UUID("11111111-2222-3333-4444-555555555555")


def test_ok_marker_wins():
    assert C(b"...\nkdive-customize-ok\n") is CustomizeVerdict.OK


def test_fail_marker():
    assert C(b"dnf: No match\nkdive-customize-failed\n") is CustomizeVerdict.FAILED


def test_genuine_oops_fails():
    assert C(b"Oops: 0000 [#1] SMP\n") is CustomizeVerdict.FAILED


def test_benign_tcg_stall_is_pending():
    assert C(b"rcu: INFO: rcu_sched detected stalls on CPUs\n") is CustomizeVerdict.PENDING
    assert C(b"watchdog: BUG: soft lockup - CPU#0 stuck for 22s!\n") is CustomizeVerdict.PENDING


def test_kernel_fatal_fault_still_fails():
    # A genuine kernel-fatal line still fails via a retained pattern (unable-to-handle-kernel).
    assert C(b"BUG: unable to handle kernel paging request\n") is CustomizeVerdict.FAILED


def test_incidental_bug_token_in_dnf_output_is_pending():
    # The customization console carries dnf transaction + scriptlet output; a bare `BUG:` token
    # (e.g. a package changelog line) must NOT false-fail the build — only the authoritative
    # ERR-trap fail marker and kernel-log-specific faults do (ADR-0345).
    assert C(b"  Updating   : foo-1.2 (fixes BUG: crash in bar)\n") is CustomizeVerdict.PENDING
    assert C(b"BUG: scheduling while atomic in a package note\n") is CustomizeVerdict.PENDING


def test_pending_when_quiet():
    assert C(b"[  ok  ] Started systemd-logind\n") is CustomizeVerdict.PENDING


class FakeDomain:
    """A transient build domain that records its force-off."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.destroyed = False
        self._active = True

    def isActive(self) -> int:  # noqa: N802 - mirrors the libvirt binding name
        return 1 if self._active else 0

    def destroy(self) -> int:
        self.destroyed = True
        self._active = False
        self.events.append("destroy")
        return 0


class FakeConn:
    """A libvirt connection that records whether it closed after the force-off."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False
        self.closed_after_force_off = False

    def createXML(  # noqa: N802, N803 - mirrors the libvirt binding names
        self, xmlDesc: str, flags: int
    ) -> FakeDomain:
        return FakeDomain(self.events)

    def close(self) -> int:
        self.closed = True
        self.closed_after_force_off = "destroy" in self.events
        return 0


def _record_create(events: list[str]) -> FakeDomain:
    events.append("create")
    return FakeDomain(events)


def _seams(*, read: bytes, settled: bool, polls: int = 10) -> CustomizationBootSeams:
    """Seams whose console read is constant and settled/poll-budget are fixed."""
    events: list[str] = []
    return CustomizationBootSeams(
        prepare_console=lambda _bid: events.append("prepare"),
        open_conn=lambda: FakeConn(events),
        create_transient=lambda _c, _x: _record_create(events),
        read_console=lambda _bid: read,
        domain_settled=lambda _bid: settled,
        sleep=lambda _s: None,
        window_polls=lambda _a: polls,
    )


def _seams_reading(read: bytes) -> CustomizationBootSeams:
    """Seams whose console read is constant; the domain never settles."""
    return _seams(read=read, settled=False)


def _seams_custom(
    *, read_console: Callable[[UUID], bytes], domain: FakeDomain
) -> CustomizationBootSeams:
    """Seams with a caller-supplied console read and a fixed transient domain."""
    return CustomizationBootSeams(
        prepare_console=lambda _bid: None,
        open_conn=lambda: FakeConn(domain.events),
        create_transient=lambda _c, _x: domain,
        read_console=read_console,
        domain_settled=lambda _bid: False,
        sleep=lambda _s: None,
        window_polls=lambda _a: 3,
    )


def test_success_seals_and_holds_conn_open_until_end():
    events: list[str] = []
    conn = FakeConn(events)
    reads = iter([b"booting...\n", b"booting...\nkdive-customize-ok\n"])
    seams = CustomizationBootSeams(
        prepare_console=lambda _bid: events.append("prepare"),
        open_conn=lambda: conn,
        create_transient=lambda _c, _x: _record_create(events),
        read_console=lambda _bid: next(reads),
        domain_settled=lambda _bid: False,
        sleep=lambda _s: events.append("sleep"),
        window_polls=lambda _a: 10,
    )
    run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)
    assert conn.closed_after_force_off is True  # conn not closed before force-off
    # ADR-0223: the console log is prepared (worker-owned 0644) BEFORE the domain is created,
    # so virtlogd truncates the existing readable file in place and a non-root worker can read it.
    assert events.index("prepare") < events.index("create")


def test_fail_marker_raises_provisioning_failure_with_tail():
    seams = _seams_reading(b"dnf error: nothing provides libfoo\nkdive-customize-failed\n")
    with pytest.raises(CategorizedError) as ei:
        run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)
    assert ei.value.category is ErrorCategory.PROVISIONING_FAILURE
    tail = ei.value.details["console_tail"]
    assert isinstance(tail, str)
    assert "libfoo" in tail


def test_genuine_fault_raises():
    seams = _seams_reading(b"Oops: 0000 [#1]\n")
    with pytest.raises(CategorizedError):
        run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)


def test_settled_without_ok_marker_fails():
    seams = _seams(read=b"partial\n", settled=True)
    with pytest.raises(CategorizedError) as ei:
        run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)
    assert ei.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_window_exhaustion_is_boot_timeout():
    seams = _seams(read=b"still booting\n", settled=False, polls=2)
    with pytest.raises(CategorizedError) as ei:
        run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)
    assert ei.value.category is ErrorCategory.BOOT_TIMEOUT


def test_unreadable_console_propagates_and_tears_down():
    # ADR-0223 root:0600 wall: the first read raises CONFIGURATION_ERROR; it must
    # propagate (not be swallowed) AND the domain must still be force-off in finally.
    domain = FakeDomain([])

    def raise_perm(_bid: UUID) -> bytes:
        raise CategorizedError(
            "failed to read console log",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )

    seams = _seams_custom(read_console=raise_perm, domain=domain)
    with pytest.raises(CategorizedError) as ei:
        run_customization_boot(BID, "<domain/>", accel="tcg", seams=seams)
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert domain.destroyed is True  # finally force-off ran despite the raise


class _RecordingGuestfish:
    """A fake `GuestfishRunner` recording `(qcow2, script)` and returning a canned stdout.

    The real runner returns the guestfish script's stdout; the seal's native ``is-file``/
    ``is-symlink`` predicates print ``true``/``false``. Default output is both ``false`` (the
    firstboot self-removed cleanly), so the seal does not raise.
    """

    def __init__(self, output: str = "false\nfalse\n") -> None:
        self.calls: list[tuple[Path, str]] = []
        self._output = output

    def __call__(self, qcow2: Path, script: str) -> str:
        self.calls.append((qcow2, script))
        return self._output


def _unit_present_guestfish(_qcow2: Path, _script: str) -> str:
    """A fake `GuestfishRunner` whose ``is-file`` reports the firstboot unit still present.

    The unit-removed assertion is now parsed from the runner's stdout (arch-safe native
    predicates), not a script abort: a ``true`` line means the unit did not self-remove.
    """
    return "true\nfalse\n"


def test_seal_script_resets_cloud_init_state(tmp_path):
    guestfish = _RecordingGuestfish()
    seal_customized_image(
        tmp_path / "img.qcow2", unit_name=CUSTOMIZE_UNIT, selinux=False, run_guestfish=guestfish
    )
    (_qcow2, script) = guestfish.calls[0]
    assert "rm-rf /var/lib/cloud/instances" in script
    assert "rm-rf /var/lib/cloud/instance" in script
    assert "rm-rf /var/lib/cloud/sem" in script
    assert "rm-rf /var/lib/cloud/data" in script


def test_seal_script_touches_autorelabel_iff_selinux():
    guestfish = _RecordingGuestfish()
    seal_customized_image(
        Path("/img.qcow2"), unit_name=CUSTOMIZE_UNIT, selinux=True, run_guestfish=guestfish
    )
    assert "touch /.autorelabel" in guestfish.calls[0][1]

    guestfish = _RecordingGuestfish()
    seal_customized_image(
        Path("/img.qcow2"), unit_name=CUSTOMIZE_UNIT, selinux=False, run_guestfish=guestfish
    )
    assert "touch /.autorelabel" not in guestfish.calls[0][1]


def test_seal_script_asserts_firstboot_unit_and_wants_symlink_are_gone():
    guestfish = _RecordingGuestfish()
    seal_customized_image(
        Path("/img.qcow2"), unit_name=CUSTOMIZE_UNIT, selinux=False, run_guestfish=guestfish
    )
    script = guestfish.calls[0][1]
    # Arch-safe native predicates (appliance ops on guest data), NOT a guest-command `sh 'test'`
    # which would exec the guest's /bin/sh in the host-arch appliance and fail cross-arch.
    assert f"is-file /etc/systemd/system/{CUSTOMIZE_UNIT}" in script
    assert f"is-symlink /etc/systemd/system/multi-user.target.wants/{CUSTOMIZE_UNIT}" in script
    assert "sh '" not in script  # no guest-command execution


def test_seal_raises_provisioning_failure_when_unit_still_present():
    with pytest.raises(CategorizedError) as ei:
        seal_customized_image(
            Path("/img.qcow2"),
            unit_name=CUSTOMIZE_UNIT,
            selinux=False,
            run_guestfish=_unit_present_guestfish,
        )
    assert ei.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert "was not self-removed" in str(ei.value)
