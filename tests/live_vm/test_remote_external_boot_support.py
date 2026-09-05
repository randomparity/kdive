"""Test-only helpers for the remote external-boot native carrier (#2121)."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Protocol

from kdive.build_artifacts.validation import parse_gnu_build_id
from kdive.providers.ports.external_boot import KernelIdentity
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    CAT_PROGRAM,
    MAX_CMDLINE_BYTES,
    OBSERVATION_PROGRAMS,
    PROC_CMDLINE_PATH,
    UNAME_PROGRAM,
)
from kdive.providers.shared.guest_agent import AgentExecResult, GuestDomain


class AgentRunner(Protocol):
    def run(
        self, domain: GuestDomain, argv: list[str], *, input_data: str | None = None
    ) -> AgentExecResult: ...


def first_differing_byte(expected: bytes, observed: bytes) -> int:
    """Return the first unequal byte offset, or the common length for a prefix mismatch."""
    return next(
        (
            offset
            for offset, pair in enumerate(zip(expected, observed, strict=False))
            if pair[0] != pair[1]
        ),
        min(len(expected), len(observed)),
    )


def assert_cmdline_equal(expected: bytes, observed: bytes) -> None:
    """Assert byte equality with the complete bounded values and first differing offset."""
    if expected == observed:
        return
    offset = first_differing_byte(expected, observed)
    raise AssertionError(
        "external-boot command line differs at byte offset "
        f"{offset}: expected={expected!r}, observed={observed!r}"
    )


def read_cmdline_early(agent: AgentRunner, domain: GuestDomain) -> bytes:
    """Read bounded `/proc/cmdline`, requiring and stripping exactly one trailing newline."""
    result = agent.run(domain, [CAT_PROGRAM, PROC_CMDLINE_PATH])
    assert result.exit_status == 0, (
        f"guest could not read /proc/cmdline (exit_status={result.exit_status})"
    )
    assert result.stdout.endswith(b"\n"), "guest /proc/cmdline read was truncated"
    value = result.stdout[:-1]
    assert len(value) <= MAX_CMDLINE_BYTES, "guest /proc/cmdline exceeded 2048 bytes"
    return value


def read_kernel_identity(agent: AgentRunner, domain: GuestDomain) -> KernelIdentity:
    """Read the identity of the matching disk-boot baseline through the production allowlist."""
    assert {UNAME_PROGRAM, CAT_PROGRAM} <= OBSERVATION_PROGRAMS
    release = agent.run(domain, [UNAME_PROGRAM, "-r"])
    machine = agent.run(domain, [UNAME_PROGRAM, "-m"])
    notes = agent.run(domain, [CAT_PROGRAM, "/sys/kernel/notes"])
    assert release.exit_status == machine.exit_status == notes.exit_status == 0
    architecture = machine.stdout.rstrip(b"\n").decode()
    assert architecture == "x86_64"
    return KernelIdentity(
        architecture="x86_64",
        release=release.stdout.rstrip(b"\n").decode(),
        gnu_build_id=parse_gnu_build_id(notes.stdout),
    )


def sha256_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def preserved_components(domain_xml: str) -> dict[str, bytes]:
    """Canonicalize the named definition components external boot must preserve."""
    root = ET.fromstring(domain_xml)
    selectors = {
        "disk": "./devices/disk",
        "network": "./devices/interface",
        "guest-agent": "./devices/channel",
        "console": "./devices/console",
        "gdbstub": "./{http://libvirt.org/schemas/domain/qemu/1.0}commandline",
        "capture": "./features/vmcoreinfo",
    }
    values: dict[str, bytes] = {}
    for name, selector in selectors.items():
        element = root.find(selector)
        assert element is not None, f"remote domain has no {name} component"
        values[name] = ET.canonicalize(ET.tostring(element, encoding="unicode")).encode()
    return values


def attempt_all_cleanup(
    actions: list[tuple[str, Callable[[], None]]], *, primary: Exception | None = None
) -> None:
    """Attempt every cleanup action and preserve the primary failure with cleanup failures."""
    failures: list[Exception] = []
    for name, action in actions:
        try:
            action()
        except Exception as exc:  # cleanup must continue across independent provider failures
            failures.append(AssertionError(f"{name}: {exc}"))
    if primary is not None:
        failures.insert(0, primary)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise ExceptionGroup("remote live carrier and cleanup failures", failures)
