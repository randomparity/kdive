from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes import provenance_probes as probes


def _completed(
    *, returncode: int = 0, stdout: str | bytes = "", stderr: str = ""
) -> subprocess.CompletedProcess[str | bytes]:
    return subprocess.CompletedProcess(
        args=["guestfish"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    results: list[subprocess.CompletedProcess[str | bytes] | BaseException],
) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    iterator: Iterator[subprocess.CompletedProcess[str | bytes] | BaseException] = iter(results)

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str | bytes]:
        calls.append((argv, kwargs))
        result = next(iterator)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(probes.subprocess, "run", fake_run)
    return calls


def test_inspect_package_versions_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, [FileNotFoundError("virt-inspector")])

    with pytest.raises(CategorizedError) as caught:
        probes.inspect_package_versions(Path("image.qcow2"))

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert caught.value.details == {"tool": "virt-inspector"}


def test_inspect_package_versions_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(
        monkeypatch,
        [subprocess.TimeoutExpired(cmd="virt-inspector", timeout=probes._VIRT_INSPECTOR_TIMEOUT_S)],
    )

    with pytest.raises(CategorizedError) as caught:
        probes.inspect_package_versions(Path("image.qcow2"))

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"timeout_s": probes._VIRT_INSPECTOR_TIMEOUT_S}


def test_inspect_package_versions_nonzero_exit_reports_stderr_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "x" * 2100
    _patch_run(monkeypatch, [_completed(returncode=2, stderr=stderr)])

    with pytest.raises(CategorizedError) as caught:
        probes.inspect_package_versions(Path("image.qcow2"))

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"stderr": stderr[-2000:]}


def test_inspect_package_versions_parses_successful_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xml = """
    <operatingsystems>
      <operatingsystem>
        <applications>
          <application><name>kernel</name><version>6.12</version></application>
          <application><name>missing-version</name></application>
        </applications>
      </operatingsystem>
    </operatingsystems>
    """
    calls = _patch_run(monkeypatch, [_completed(stdout=xml)])

    assert probes.inspect_package_versions(Path("image.qcow2")) == {"kernel": "6.12"}
    assert calls == [
        (
            ["virt-inspector", "--no-icon", "-a", "image.qcow2"],
            {
                "capture_output": True,
                "text": True,
                "timeout": probes._VIRT_INSPECTOR_TIMEOUT_S,
                "check": False,
            },
        )
    ]


type _PathProbe = Callable[[Path], object]


@pytest.mark.parametrize(
    "probe",
    [
        probes.probe_makedumpfile_marker,
        probes.probe_drgn_marker,
        lambda path: probes.probe_kernel_config(path, "6.12.0"),
        probes.probe_boot_entries,
        probes.probe_os_release,
    ],
)
def test_guestfish_probes_missing_executable(
    probe: _PathProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_run(monkeypatch, [FileNotFoundError("guestfish")])

    with pytest.raises(CategorizedError) as caught:
        probe(Path("image.qcow2"))

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert caught.value.details == {"tool": "guestfish"}


@pytest.mark.parametrize(
    "probe",
    [
        probes.probe_makedumpfile_marker,
        probes.probe_drgn_marker,
        lambda path: probes.probe_kernel_config(path, "6.12.0"),
        probes.probe_boot_entries,
        probes.probe_os_release,
    ],
)
def test_guestfish_probes_timeout(probe: _PathProbe, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(
        monkeypatch,
        [subprocess.TimeoutExpired(cmd="guestfish", timeout=probes._GUESTFISH_TIMEOUT_S)],
    )

    with pytest.raises(CategorizedError) as caught:
        probe(Path("image.qcow2"))

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"timeout_s": probes._GUESTFISH_TIMEOUT_S}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_completed(returncode=1), None),
        (_completed(stdout=" \n"), None),
        (_completed(stdout="makedumpfile 1.7.5\n"), "makedumpfile 1.7.5"),
    ],
)
def test_probe_makedumpfile_marker_missing_and_success_outputs(
    result: subprocess.CompletedProcess[str | bytes],
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, [result])

    assert probes.probe_makedumpfile_marker(Path("image.qcow2")) == expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_completed(returncode=1), None),
        (_completed(stdout=" \n"), None),
        (_completed(stdout="drgn 0.0.31\n"), "drgn 0.0.31"),
    ],
)
def test_probe_drgn_marker_missing_and_success_outputs(
    result: subprocess.CompletedProcess[str | bytes],
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_run(monkeypatch, [result])

    assert probes.probe_drgn_marker(Path("image.qcow2")) == expected
    assert calls[0][0][-1] == probes.DRGN_MARKER_GUEST_PATH


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_completed(returncode=1, stdout=b"CONFIG_X=y\n"), None),
        (_completed(stdout=b""), None),
        (_completed(stdout=b"CONFIG_X=y\n"), b"CONFIG_X=y\n"),
    ],
)
def test_probe_kernel_config_missing_and_success_outputs(
    result: subprocess.CompletedProcess[str | bytes],
    expected: bytes | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, [result])

    assert probes.probe_kernel_config(Path("image.qcow2"), "6.12.0") == expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_completed(returncode=1, stdout="vmlinuz-6.12\n"), None),
        (_completed(stdout=""), []),
        (
            _completed(stdout=" vmlinuz-6.12 \n\n initramfs-6.12.img\n"),
            ["vmlinuz-6.12", "initramfs-6.12.img"],
        ),
    ],
)
def test_probe_boot_entries_missing_empty_and_success_outputs(
    result: subprocess.CompletedProcess[str | bytes],
    expected: list[str] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, [result])

    assert probes.probe_boot_entries(Path("image.qcow2")) == expected


def test_probe_os_release_falls_back_to_usr_lib(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(
        monkeypatch,
        [
            _completed(returncode=1),
            _completed(stdout='ID="fedora"\nVERSION_ID="43"\n'),
        ],
    )

    assert probes.probe_os_release(Path("image.qcow2")) == 'ID="fedora"\nVERSION_ID="43"\n'
    assert [call[0][-1] for call in calls] == ["/etc/os-release", "/usr/lib/os-release"]


def test_probe_os_release_falls_back_after_empty_etc_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, [_completed(stdout="\n"), _completed(stdout='ID="rhel"\n')])

    assert probes.probe_os_release(Path("image.qcow2")) == 'ID="rhel"\n'


def test_probe_os_release_returns_none_when_both_paths_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, [_completed(returncode=1), _completed(returncode=1)])

    assert probes.probe_os_release(Path("image.qcow2")) is None
