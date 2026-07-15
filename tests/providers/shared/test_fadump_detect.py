"""Unit tests for the host pseries-fadump discovery probe (ADR-0349, #1151)."""

from __future__ import annotations

import subprocess

import pytest

from kdive.providers.shared.fadump_detect import (
    PSERIES_FADUMP_QEMU_FLOOR,
    VersionRunner,
    detect_pseries_fadump,
)

_PPC = "ppc64le"


def _arches(emulator: str | None) -> dict[str, dict[str, str]]:
    arches: dict[str, dict[str, str]] = {
        "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-x86"}
    }
    if emulator is not None:
        arches[_PPC] = {"accel": "tcg", "emulator": emulator}
    return arches


def _version(text: str) -> VersionRunner:
    def _run(_argv: list[str]) -> str:
        return text

    return _run


def test_floor_is_qemu_10_2() -> None:
    assert PSERIES_FADUMP_QEMU_FLOOR == (10, 2)


@pytest.mark.parametrize(
    "version_line",
    [
        "QEMU emulator version 10.2.2 (qemu-10.2.2-1.fc44)",
        "QEMU emulator version 10.2.0",
        "QEMU emulator version 10.3.0 (some build)",
        "QEMU emulator version 11.0.0",
    ],
)
def test_supported_at_or_above_the_floor(version_line: str) -> None:
    arches = _arches("/usr/bin/qemu-system-ppc64")
    assert detect_pseries_fadump(arches, run_version=_version(version_line)) is True


@pytest.mark.parametrize(
    "version_line",
    [
        "QEMU emulator version 10.1.0 (qemu-10.1.0)",
        "QEMU emulator version 9.2.1",
        "QEMU emulator version 8.0.0",
    ],
)
def test_unsupported_below_the_floor(version_line: str) -> None:
    arches = _arches("/usr/bin/qemu-system-ppc64")
    assert detect_pseries_fadump(arches, run_version=_version(version_line)) is False


def test_false_when_no_ppc64le_guest_arch() -> None:
    # No ppc64le emulator advertised: fadump is N/A, and no subprocess should be attempted.
    def _explode(_argv: list[str]) -> str:  # pragma: no cover - must not be called
        raise AssertionError("probe must not spawn when there is no ppc64le emulator")

    assert detect_pseries_fadump(_arches(None), run_version=_explode) is False


@pytest.mark.parametrize(
    "boom",
    [
        FileNotFoundError("no qemu"),
        subprocess.CalledProcessError(1, "qemu"),
        subprocess.TimeoutExpired("qemu", 5),
        OSError("boom"),
    ],
)
def test_false_when_probe_fails(boom: Exception) -> None:
    # Fail-closed on every probe error path (ADR-0349): uncertainty denies fadump.
    def _raise(_argv: list[str]) -> str:
        raise boom

    assert detect_pseries_fadump(_arches("/usr/bin/qemu-system-ppc64"), run_version=_raise) is False


@pytest.mark.parametrize("garbage", ["", "not a version", "QEMU emulator version ten.two"])
def test_false_on_unparseable_version(garbage: str) -> None:
    arches = _arches("/usr/bin/qemu-system-ppc64")
    assert detect_pseries_fadump(arches, run_version=_version(garbage)) is False
