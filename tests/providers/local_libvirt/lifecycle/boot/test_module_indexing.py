"""Host-side kernel-module indexing for cross-arch installs (#1148, ADR-0346).

The guest kernel writer used to run the guest's own ``depmod`` inside libguestfs's host-arch
appliance, which fails for a ppc64le module tree under an x86_64 appliance (``Exec format error``,
#1146). Indexing moved host-side: ``depmod`` parses ELF and never executes it, so the host's
``depmod -b`` indexes a foreign-arch tree correctly. These tests drive that pure host-side helper
(no libguestfs) and its failure categories.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.boot import guest_kernel_writer as gkw
from kdive.providers.local_libvirt.lifecycle.boot import kernel_bundle
from kdive.providers.local_libvirt.lifecycle.boot.guest_kernel_writer import index_modules_tar

# A ppc64le uname — the arch suffix must survive indexing unchanged (the cross-arch case #1148 fix).
_VERSION = "6.19.10-300.fc44.ppc64le"


def _modules_tar(tmp_path: Path, version: str) -> Path:
    """A modules-only gzip tar with one fake .ko under ``lib/modules/<version>/`` (repack shape)."""
    tar_path = tmp_path / "modules.tar.gz"
    data = b"\x7fELF\x02\x01 fake ppc64le ko"
    with tarfile.open(tar_path, "w:gz") as out:
        info = tarfile.TarInfo(f"lib/modules/{version}/kernel/drivers/foo.ko")
        info.size = len(data)
        out.addfile(info, io.BytesIO(data))
    return tar_path


def _modules_tar_with_build_symlink(tmp_path: Path, version: str) -> Path:
    """A modules tar that also carries the ``build`` symlink to an absolute path.

    Every real kernel module tree has ``lib/modules/<ver>/build`` (and ``source``) as a symlink to
    an absolute path like ``/usr/src/kernels/<ver>``. Python's ``data`` extraction filter rejects
    such absolute-symlink targets (``AbsoluteLinkError``); the ``tar`` filter allows them while
    still blocking path traversal. Regression guard for the failure the #1148 live proof surfaced.
    """
    tar_path = tmp_path / "modules-with-build.tar.gz"
    data = b"\x7fELF\x02\x01 fake ppc64le ko"
    with tarfile.open(tar_path, "w:gz") as out:
        ko = tarfile.TarInfo(f"lib/modules/{version}/kernel/drivers/foo.ko")
        ko.size = len(data)
        out.addfile(ko, io.BytesIO(data))
        link = tarfile.TarInfo(f"lib/modules/{version}/build")
        link.type = tarfile.SYMTYPE
        link.linkname = f"/usr/src/kernels/{version}"
        out.addfile(link)
    return tar_path


def test_index_modules_tar_skips_unsafe_build_symlink(tmp_path: Path) -> None:
    modules_tar = _modules_tar_with_build_symlink(tmp_path, _VERSION)

    def fake_depmod(*, basedir: Path, version: str) -> None:
        dep = basedir / "lib" / "modules" / version / "modules.dep"
        dep.parent.mkdir(parents=True, exist_ok=True)
        dep.write_text("kernel/drivers/foo.ko:\n")

    workdir = tmp_path / "work"
    workdir.mkdir()
    # Must not raise AbsoluteLinkError: the absolute build symlink is skipped (a root symlink-escape
    # risk, and unneeded for depmod/kdump), while the real modules still extract and index.
    indexed = index_modules_tar(modules_tar, _VERSION, workdir=workdir, run_depmod=fake_depmod)
    with tarfile.open(indexed, "r:gz") as archive:
        names = set(archive.getnames())
    assert f"lib/modules/{_VERSION}/build" not in names, "unsafe absolute symlink was extracted"
    assert f"lib/modules/{_VERSION}/kernel/drivers/foo.ko" in names
    assert f"lib/modules/{_VERSION}/modules.dep" in names


def test_validate_release_accepts_a_real_ppc64le_release() -> None:
    assert gkw._validate_release("6.19.10-300.fc44.ppc64le") == "6.19.10-300.fc44.ppc64le"


@pytest.mark.parametrize(
    "bad",
    ["-n", "..", "a;b", "a b", "/etc/passwd", "foo/../bar", "", "x" * 200, "$(id)"],
)
def test_validate_release_rejects_hostile_names(bad: str) -> None:
    # The release is parsed from a semi-trusted tar and reaches a root depmod arg + guest paths, so
    # a depmod option (-n), a path fragment, whitespace, or a shell metachar is rejected (#1148).
    with pytest.raises(CategorizedError) as exc:
        gkw._validate_release(bad)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_index_modules_tar_rejects_a_traversal_link(tmp_path: Path) -> None:
    # A path-traversal symlink (escaping the destination) is hostile — a real module tree never has
    # one — so it is rejected (CONFIGURATION_ERROR), not silently skipped like an absolute
    # build/source symlink (#1148 review).
    tar_path = tmp_path / "evil.tar.gz"
    with tarfile.open(tar_path, "w:gz") as out:
        link = tarfile.TarInfo(f"lib/modules/{_VERSION}/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../../../../../etc/evil"
        out.addfile(link)
    workdir = tmp_path / "work"
    workdir.mkdir()
    with pytest.raises(CategorizedError) as exc:
        index_modules_tar(tar_path, _VERSION, workdir=workdir, run_depmod=lambda **_kw: None)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_index_modules_tar_runs_depmod_and_repacks_with_dep(tmp_path: Path) -> None:
    modules_tar = _modules_tar(tmp_path, _VERSION)

    def fake_depmod(*, basedir: Path, version: str) -> None:
        # Stand in for real depmod: write a modules.dep the way `depmod -b basedir version` would.
        dep = basedir / "lib" / "modules" / version / "modules.dep"
        dep.parent.mkdir(parents=True, exist_ok=True)
        dep.write_text("kernel/drivers/foo.ko:\n")

    workdir = tmp_path / "work"
    workdir.mkdir()
    indexed = index_modules_tar(modules_tar, _VERSION, workdir=workdir, run_depmod=fake_depmod)

    with tarfile.open(indexed, "r:gz") as archive:
        names = set(archive.getnames())
    # The re-tarred tree carries both the generated modules.dep (arch suffix intact) and the .ko —
    # tar_in of this into the guest injects a ready-indexed module tree, no in-guest depmod.
    assert f"lib/modules/{_VERSION}/modules.dep" in names
    assert f"lib/modules/{_VERSION}/kernel/drivers/foo.ko" in names


def test_index_modules_tar_rejects_oversize_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A gzip/tar bomb must not extract unbounded as root on the worker host: the cumulative
    # uncompressed size is capped (#1148 review). Shrink the cap so the small fixture trips it.
    modules_tar = _modules_tar(tmp_path, _VERSION)
    monkeypatch.setattr(kernel_bundle, "MAX_KERNEL_TAR_UNCOMPRESSED_BYTES", 4)
    workdir = tmp_path / "work"
    workdir.mkdir()
    with pytest.raises(CategorizedError) as exc:
        index_modules_tar(modules_tar, _VERSION, workdir=workdir, run_depmod=lambda **_kw: None)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_index_modules_tar_rejects_too_many_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modules_tar = _modules_tar(tmp_path, _VERSION)
    monkeypatch.setattr(kernel_bundle, "MAX_KERNEL_TAR_MEMBERS", 0)
    workdir = tmp_path / "work"
    workdir.mkdir()
    with pytest.raises(CategorizedError) as exc:
        index_modules_tar(modules_tar, _VERSION, workdir=workdir, run_depmod=lambda **_kw: None)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_index_modules_tar_fails_when_depmod_produces_no_dep(tmp_path: Path) -> None:
    modules_tar = _modules_tar(tmp_path, _VERSION)

    def noop_depmod(*, basedir: Path, version: str) -> None:
        # A depmod that "succeeds" but writes no modules.dep must not pass silently.
        return None

    workdir = tmp_path / "work"
    workdir.mkdir()
    with pytest.raises(CategorizedError) as exc:
        index_modules_tar(modules_tar, _VERSION, workdir=workdir, run_depmod=noop_depmod)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def _stub_which(monkeypatch: pytest.MonkeyPatch, resolved: str | None) -> dict[str, object]:
    """Point the fixed-list lookup at ``resolved``, capturing the name and search path it used."""
    captured: dict[str, object] = {}

    def fake_which(name: str, path: str | None = None) -> str | None:
        captured["which"] = (name, path)
        return resolved

    monkeypatch.setattr(gkw.shutil, "which", fake_which)
    return captured


def test_run_host_depmod_runs_the_resolved_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_which(monkeypatch, "/usr/sbin/depmod")

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gkw.subprocess, "run", fake_run)
    gkw._run_host_depmod(basedir=tmp_path, version=_VERSION)
    # The absolute path, never the bare name: the systemd worker gate execs without PATH, so a
    # bare name resolves through os.defpath (/bin:/usr/bin), which omits /usr/sbin (#2300).
    assert captured["args"] == ["/usr/sbin/depmod", "-b", str(tmp_path), _VERSION]
    # The search is restricted to the fixed host-tool list, not the inherited environment.
    assert captured["which"] == ("depmod", "/usr/sbin:/usr/bin:/sbin:/bin")


def test_run_host_depmod_unresolvable_names_searched_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_which(monkeypatch, None)
    with pytest.raises(CategorizedError) as exc:
        gkw._run_host_depmod(basedir=tmp_path, version=_VERSION)
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    # The searched set is in the envelope so the failure is diagnosable without host access.
    assert exc.value.details.get("searched") == ["/usr/sbin", "/usr/bin", "/sbin", "/bin"]
    # Both remedies, because the binary may be genuinely absent *or* merely outside the searched
    # set — asserting "install kmod" alone is the false remedy #2300 was filed about.
    assert "kmod" in str(exc.value)
    assert "KDIVE_DEPMOD" in str(exc.value)


def test_run_host_depmod_uses_the_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "depmod"
    override.write_text("#!/bin/sh\nexit 0\n")
    override.chmod(0o755)
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    def unreachable_which(*_args: object, **_kwargs: object) -> str | None:
        raise AssertionError("an explicit override must short-circuit the fixed-list search")

    monkeypatch.setattr(gkw.subprocess, "run", fake_run)
    monkeypatch.setattr(gkw.shutil, "which", unreachable_which)
    monkeypatch.setenv("KDIVE_DEPMOD", str(override))
    gkw._run_host_depmod(basedir=tmp_path, version=_VERSION)
    assert captured["args"] == [str(override), "-b", str(tmp_path), _VERSION]


@pytest.mark.parametrize("kind", ["relative", "missing", "not-executable"])
def test_override_must_be_absolute_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "relative":
        value = "usr/sbin/depmod"
    elif kind == "missing":
        value = str(tmp_path / "absent")
    else:
        plain = tmp_path / "plain"
        plain.write_text("")
        plain.chmod(0o644)
        value = str(plain)
    monkeypatch.setenv("KDIVE_DEPMOD", value)
    with pytest.raises(CategorizedError) as exc:
        gkw._run_host_depmod(basedir=tmp_path, version=_VERSION)
    # The operator's own value being wrong is a misconfiguration, not a missing package — telling
    # them to install kmod here would repeat the defect #2300 exists to remove.
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details.get("variable") == "KDIVE_DEPMOD"


@pytest.mark.parametrize("kind", ["vanished", "wrong-format"])
def test_unexecutable_depmod_is_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "vanished":
        # Resolved and then removed before the exec: the window which()-then-run() opens.
        resolved = str(tmp_path / "gone")
    else:
        # A mode-0755 file with no shebang: execve fails ENOEXEC, which CPython raises as a bare
        # OSError rather than FileNotFoundError, so catching only the latter lets it escape the
        # envelope uncategorized.
        script = tmp_path / "not-elf"
        script.write_text("not an executable\n")
        script.chmod(0o755)
        resolved = str(script)
    _stub_which(monkeypatch, resolved)
    with pytest.raises(CategorizedError) as exc:
        gkw._run_host_depmod(basedir=tmp_path, version=_VERSION)
    # Non-retryable, matching the disposition a missing depmod has always had, so the job
    # dead-letters instead of retrying a binary that will not become executable.
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert exc.value.details.get("depmod") == resolved


def test_run_host_depmod_nonzero_surfaces_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_which(monkeypatch, "/usr/sbin/depmod")

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, returncode=1, stdout="", stderr="depmod: ERROR: bad module signature"
        )

    monkeypatch.setattr(gkw.subprocess, "run", fake_run)
    with pytest.raises(CategorizedError) as exc:
        gkw._run_host_depmod(basedir=tmp_path, version=_VERSION)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    # The #1146 diagnosability note: the depmod cause is legible from the tool envelope details.
    assert "bad module signature" in str(exc.value.details.get("depmod_stderr", ""))
