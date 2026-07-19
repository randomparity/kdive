"""Unit tests for the in-process remote-libvirt rootfs build plane (M2.4/3, ADR-0080, ADR-0092).

These cover the plane's orchestration and provenance contract, plus the ``virt-builder``
tool-boundary error mapping, without libguestfs, qemu, or the network:

- ``_real_virt_builder``/``_run_libguestfs_tool`` are exercised directly by mocking
  ``subprocess.run`` at the boundary ``kdive.images.planes._build_common`` calls (the generic
  ``run_guestfs_tool`` exception-mapping is already covered by
  ``tests/images/planes/test_build_common.py``; this file only guards this module's own argv
  construction and the fixed ``missing_message``/``stage`` it passes through).
- ``RemoteLibvirtRootfsBuildPlane.build()`` is exercised via the injected ``virt_builder``/
  ``inspect_versions`` seams (:class:`RemoteRootfsBuildTools`), mirroring
  ``tests/providers/local_libvirt/test_rootfs_build.py``.

The real libguestfs path is exercised on the operator-run live-stack path.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes import _build_common
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildSpec
from kdive.providers.remote_libvirt.rootfs_build import (
    RemoteLibvirtRootfsBuildPlane,
    RemoteRootfsBuildTools,
    _guest_agent_packages,
    _real_virt_builder,
)
from kdive.providers.shared.build_timeouts import SLOW_BUILD_TOOL_TIMEOUT_S


def _spec(**overrides: object) -> RootfsBuildSpec:
    base: dict[str, object] = {
        "provider": "remote-libvirt",
        "name": "fedora-remote-43",
        "arch": "x86_64",
        "releasever": "43",
        "packages": ("openssh-server", "drgn"),
        "source_image_digest": "ignored:caller-declared",
        "capabilities": ("agent", "kdump", "drgn"),
    }
    base.update(overrides)
    return RootfsBuildSpec(**base)  # ty: ignore[invalid-argument-type]


# --- _guest_agent_packages ---------------------------------------------------------------------


def test_guest_agent_packages_prepended_when_absent() -> None:
    assert _guest_agent_packages(("openssh-server", "drgn")) == (
        "qemu-guest-agent",
        "openssh-server",
        "drgn",
    )


def test_guest_agent_packages_unchanged_when_already_present() -> None:
    # Present anywhere in the tuple -> returned as-is, not duplicated or reordered.
    assert _guest_agent_packages(("drgn", "qemu-guest-agent")) == ("drgn", "qemu-guest-agent")


# --- _real_virt_builder / _run_libguestfs_tool (subprocess boundary) --------------------------


def test_real_virt_builder_invokes_the_fixed_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_build_common.subprocess, "run", _run)
    qcow2 = tmp_path / "scratch.qcow2"

    _real_virt_builder(
        releasever="43", packages=("drgn", "openssh-server"), qcow2=qcow2, size="12G"
    )

    assert len(calls) == 1
    assert calls[0]["argv"] == [
        "virt-builder",
        "fedora-43",
        "--format",
        "qcow2",
        "--size",
        "12G",
        "--output",
        str(qcow2),
        "--install",
        "qemu-guest-agent,drgn,openssh-server",
        "--run-command",
        "systemctl enable qemu-guest-agent.service",
    ]
    assert calls[0]["timeout"] == SLOW_BUILD_TOOL_TIMEOUT_S


def test_real_virt_builder_maps_missing_tool_to_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _missing(argv: list[str], **kwargs: object) -> NoReturn:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(_build_common.subprocess, "run", _missing)

    with pytest.raises(CategorizedError) as caught:
        _real_virt_builder(
            releasever="43", packages=("drgn",), qcow2=tmp_path / "out.qcow2", size="10G"
        )

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert str(caught.value) == "virt-builder is not installed; cannot build the remote base image"
    assert caught.value.details == {"stage": "virt-builder", "tool": "virt-builder"}


def test_real_virt_builder_maps_nonzero_exit_to_provisioning_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _failed(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, returncode=1, stdout="", stderr="dnf: no such repo"
        )

    monkeypatch.setattr(_build_common.subprocess, "run", _failed)

    with pytest.raises(CategorizedError) as caught:
        _real_virt_builder(
            releasever="43", packages=("drgn",), qcow2=tmp_path / "out.qcow2", size="10G"
        )

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert str(caught.value) == "virt-builder failed"
    assert caught.value.details["stage"] == "virt-builder"


def test_real_virt_builder_maps_launch_oserror_to_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _oserror(argv: list[str], **kwargs: object) -> NoReturn:
        raise PermissionError(argv[0])

    monkeypatch.setattr(_build_common.subprocess, "run", _oserror)

    with pytest.raises(CategorizedError) as caught:
        _real_virt_builder(
            releasever="43", packages=("drgn",), qcow2=tmp_path / "out.qcow2", size="10G"
        )

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(caught.value) == "failed to launch virt-builder for virt-builder"
    assert caught.value.details == {
        "stage": "virt-builder",
        "tool": "virt-builder",
        "error": "PermissionError",
    }


# --- Plane orchestration (tool-seam fakes) -----------------------------------------------------


def _no_versions(_qcow2: Path) -> dict[str, str]:
    return {}


@dataclass
class _Recorder:
    """A ``virt_builder`` stub recording its calls; writes the scratch qcow2 unless told not to."""

    order: list[str] = field(default_factory=list)
    virt_builder_calls: list[dict[str, object]] = field(default_factory=list)
    payload: bytes = b"remote-qcow2-bytes"
    write_output: bool = True

    def virt_builder(
        self, *, releasever: str, packages: tuple[str, ...], qcow2: Path, size: str
    ) -> None:
        self.order.append("virt-builder")
        self.virt_builder_calls.append(
            {"releasever": releasever, "packages": packages, "qcow2": qcow2, "size": size}
        )
        if self.write_output:
            qcow2.write_bytes(self.payload)


def _tools(rec: _Recorder, inspect_versions: object = _no_versions) -> RemoteRootfsBuildTools:
    return RemoteRootfsBuildTools(
        virt_builder=rec.virt_builder,
        inspect_versions=inspect_versions,  # ty: ignore[invalid-argument-type]
    )


def _plane(
    tmp_path: Path, rec: _Recorder, inspect_versions: object = _no_versions
) -> RemoteLibvirtRootfsBuildPlane:
    return RemoteLibvirtRootfsBuildPlane(
        workspace=tmp_path / "work", tools=_tools(rec, inspect_versions)
    )


def test_default_workspace_is_the_managed_build_path() -> None:
    assert RemoteLibvirtRootfsBuildPlane()._workspace == Path("/var/lib/kdive/build/images")
    assert RemoteLibvirtRootfsBuildPlane.from_env()._workspace == Path(
        "/var/lib/kdive/build/images"
    )


def test_build_produces_qcow2_with_content_digest(tmp_path: Path) -> None:
    rec = _Recorder(payload=b"the-remote-image-bytes")
    out = _plane(tmp_path, rec).build(_spec())

    assert isinstance(out, RootfsBuildOutput)
    assert out.qcow2_path.exists()
    assert out.qcow2_path.read_bytes() == b"the-remote-image-bytes"
    expected = "sha256:" + hashlib.sha256(b"the-remote-image-bytes").hexdigest()
    assert out.digest == expected, "image identity is the qcow2 content digest"


def test_build_passes_the_spec_and_configured_size_to_virt_builder(tmp_path: Path) -> None:
    rec = _Recorder()
    RemoteLibvirtRootfsBuildPlane(workspace=tmp_path / "work", size="20G", tools=_tools(rec)).build(
        _spec(releasever="44", packages=("drgn",))
    )

    assert rec.virt_builder_calls == [
        {
            "releasever": "44",
            "packages": ("drgn",),
            "qcow2": rec.virt_builder_calls[0]["qcow2"],
            "size": "20G",
        }
    ]


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", ".hidden", "-leading", "with space"])
def test_build_rejects_a_name_that_would_escape_the_workspace(
    tmp_path: Path, bad_name: str
) -> None:
    rec = _Recorder()
    with pytest.raises(CategorizedError) as exc:
        _plane(tmp_path, rec).build(_spec(name=bad_name))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert rec.order == [], "an unsafe name is rejected before virt-builder runs"


def test_build_missing_output_raises_provisioning_failure(tmp_path: Path) -> None:
    # virt-builder "succeeds" (raises nothing) but leaves no scratch file behind.
    rec = _Recorder(write_output=False)
    with pytest.raises(CategorizedError) as exc:
        _plane(tmp_path, rec).build(_spec())
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert str(exc.value) == "virt-builder reported success but produced no image"
    assert exc.value.details == {"stage": "virt-builder"}


def test_build_propagates_virt_builder_missing_dependency(tmp_path: Path) -> None:
    def _boom(**_kwargs: object) -> NoReturn:
        raise CategorizedError(
            "virt-builder is not installed; cannot build the remote base image",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"stage": "virt-builder", "tool": "virt-builder"},
        )

    tools = RemoteRootfsBuildTools(virt_builder=_boom)
    plane = RemoteLibvirtRootfsBuildPlane(workspace=tmp_path / "work", tools=tools)
    with pytest.raises(CategorizedError) as exc:
        plane.build(_spec())
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_build_propagates_virt_builder_infrastructure_failure(tmp_path: Path) -> None:
    def _boom(**_kwargs: object) -> NoReturn:
        raise CategorizedError(
            "failed to launch virt-builder for virt-builder",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stage": "virt-builder", "tool": "virt-builder", "error": "PermissionError"},
        )

    tools = RemoteRootfsBuildTools(virt_builder=_boom)
    plane = RemoteLibvirtRootfsBuildPlane(workspace=tmp_path / "work", tools=tools)
    with pytest.raises(CategorizedError) as exc:
        plane.build(_spec())
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


# --- Provenance / _capture_versions -------------------------------------------------------------


def test_provenance_full_shape(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec).build(_spec())

    assert out.provenance == {
        "plane": "remote-libvirt",
        "boot_method": "disk-image",
        "releasever": "43",
        "packages": ["qemu-guest-agent", "openssh-server", "drgn"],
        "source_image_digest": "ignored:caller-declared",
        "capabilities": ["agent", "kdump", "drgn"],
        "arch": "x86_64",
        "image_size": "10G",
        "guest_access_seam": "qemu-guest-agent",
    }


def test_provenance_packages_include_guest_agent_even_when_not_requested(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec).build(_spec(packages=("drgn",)))
    assert out.provenance["packages"] == ["qemu-guest-agent", "drgn"]


def test_provenance_records_package_versions_filtered_to_the_wanted_set(tmp_path: Path) -> None:
    # The inspector reports a superset; provenance keeps only qemu-guest-agent + requested.
    rec = _Recorder()
    versions = {
        "qemu-guest-agent": "108",
        "openssh-server": "9.6",
        "drgn": "0.0.28",
        "glibc": "2.39",
    }
    out = _plane(tmp_path, rec, inspect_versions=lambda _q: versions).build(_spec())
    assert out.provenance["package_versions"] == {
        "qemu-guest-agent": "108",
        "openssh-server": "9.6",
        "drgn": "0.0.28",
    }


def test_provenance_versions_absent_for_unreported_request(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec, inspect_versions=lambda _q: {"drgn": "0.0.28"}).build(_spec())
    assert out.provenance["package_versions"] == {"drgn": "0.0.28"}


def test_capture_versions_degrades_to_empty_on_inspector_failure(tmp_path: Path) -> None:
    def _boom(_q: Path) -> dict[str, str]:
        raise CategorizedError("no tool", category=ErrorCategory.MISSING_DEPENDENCY)

    rec = _Recorder()
    out = _plane(tmp_path, rec, inspect_versions=_boom).build(_spec())
    assert "package_versions" not in out.provenance, (
        "a failed capture degrades to an empty map, which to_dict() omits (ADR-0252)"
    )


def test_capture_versions_logs_a_warning_on_inspector_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(_q: Path) -> dict[str, str]:
        raise CategorizedError("no tool", category=ErrorCategory.MISSING_DEPENDENCY)

    rec = _Recorder()
    with caplog.at_level(logging.WARNING):
        _plane(tmp_path, rec, inspect_versions=_boom).build(_spec())

    assert any(
        "package-version capture failed; provenance omits package_versions" in record.message
        for record in caplog.records
    )


def test_capture_versions_does_not_swallow_a_non_categorized_error(tmp_path: Path) -> None:
    # Only CategorizedError degrades to {}; a programming-error exception must still propagate.
    def _boom(_q: Path) -> dict[str, str]:
        raise ValueError("unexpected")

    rec = _Recorder()
    with pytest.raises(ValueError, match="unexpected"):
        _plane(tmp_path, rec, inspect_versions=_boom).build(_spec())
