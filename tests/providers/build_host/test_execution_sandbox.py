"""execution.py run-steps route through the sandbox chokepoint (ADR-0214)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.build_host import execution as ex
from kdive.providers.shared.build_host import sandbox as sb
from kdive.security.secrets.secret_registry import SecretRegistry


def _box() -> sb.BuildSandbox:
    return sb.BuildSandbox(uid=7, gid=7, extra_groups=(7,), user_name="b", home="/home/b")


class _R:
    returncode = 0
    stdout = ""
    stderr = ""


def test_run_make_passes_sandbox_to_chokepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_sandbox_run(sandbox, argv, **kwargs):
        seen["sandbox"] = sandbox
        seen["argv"] = argv
        return _R()

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    box = _box()
    assert ex.real_run_make(Path("/ws"), sandbox=box, registry=SecretRegistry()).returncode == 0
    assert seen["sandbox"] is box
    assert seen["argv"][0] == "make"


def test_run_make_default_sandbox_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_sandbox_run(sandbox, argv, **kwargs):
        seen.setdefault("sandbox", sandbox)
        return _R()

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    ex.real_run_make(Path("/ws"), registry=SecretRegistry())
    assert seen["sandbox"] is None


def test_olddefconfig_threads_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_sandbox_run(sandbox, argv, **kwargs):
        seen.update(sandbox=sandbox, argv=argv)
        return _R()

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    box = _box()
    ex.real_run_olddefconfig(Path("/ws"), sandbox=box, registry=SecretRegistry())
    assert seen["sandbox"] is box
    assert "olddefconfig" in seen["argv"]


def test_modules_install_threads_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_sandbox_run(sandbox, argv, **kwargs):
        seen.update(sandbox=sandbox, argv=argv)
        return _R()

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    box = _box()
    ex.real_run_modules_install(Path("/ws"), Path("/mod"), sandbox=box, registry=SecretRegistry())
    assert seen["sandbox"] is box
    assert "modules_install" in seen["argv"]


def test_read_build_id_extracts_through_sandbox_chokepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """objcopy runs demoted via sandbox_run when a sandbox is active (ADR-0214, #838).

    The root worker must not parse the build's attacker-influenced vmlinux ELF directly; the
    note output file is first handed to the build user so the demoted objcopy can write it.
    """
    seen: dict = {}
    chowned: list = []

    def fake_sandbox_run(sandbox, argv, **kwargs):
        seen.update(sandbox=sandbox, argv=argv)
        return _R()

    box = _box()
    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    monkeypatch.setattr(sb.os, "chown", lambda p, u, g, **kw: chowned.append(p))
    monkeypatch.setattr(ex, "parse_gnu_build_id", lambda _notes: "deadbeef")
    monkeypatch.setattr(ex, "read_bytes_nofollow", lambda _p, **_kw: b"notes")

    assert ex.real_read_build_id(Path("/ws"), box) == "deadbeef"
    assert seen["sandbox"] is box
    assert seen["argv"][0] == "objcopy"
    assert chowned and chowned[0] == seen["argv"][-1]  # note file handed to the build user first


def test_read_bytes_nofollow_reads_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "notes"
    target.write_bytes(b"build-id-notes")
    assert (
        ex.read_bytes_nofollow(target, category=ErrorCategory.BUILD_FAILURE, output="vmlinux notes")
        == b"build-id-notes"
    )


def test_read_bytes_nofollow_refuses_a_swapped_in_symlink(tmp_path: Path) -> None:
    """A build-user symlink swap of the note file cannot redirect the root read at another file.

    The demoted-objcopy path hands the note file to the build user; without O_NOFOLLOW a swapped
    symlink would let the root read-back follow it to an arbitrary file (ADR-0214, #838).
    """
    secret = tmp_path / "root-only"
    secret.write_bytes(b"root secret bytes")
    note = tmp_path / "vmlinux.note"
    note.symlink_to(secret)

    with pytest.raises(CategorizedError) as e:
        ex.read_bytes_nofollow(note, category=ErrorCategory.BUILD_FAILURE, output="vmlinux notes")
    assert e.value.details["output"] == "vmlinux notes"
