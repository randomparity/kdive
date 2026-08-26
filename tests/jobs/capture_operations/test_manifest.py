"""Build/install and deployment guards for capture bootstrap attestation (ADR-0558)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kdive.jobs.capture_operations import bootstrap_attestation, bootstrap_elf
from kdive.jobs.capture_operations.launcher import verify_capture_bootstrap_manifest

_ROOT = Path(__file__).parents[3]
_SCRIPT = _ROOT / "scripts/build-capture-bootstrap-manifest.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args], text=True, capture_output=True, check=False
    )


def _compile_runpath_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("C compiler is required for the ELF RUNPATH fixture")
    selected_dir = tmp_path / "selected"
    unused_dir = tmp_path / "unused"
    selected_dir.mkdir()
    unused_dir.mkdir()
    source = tmp_path / "choice.c"
    source.write_text("int choice(void) { return CHOICE; }\n")
    selected = selected_dir / "libkdive-choice.so.1"
    unused = unused_dir / "libkdive-choice.so.1"
    for destination, choice in ((selected, "7"), (unused, "9")):
        subprocess.run(
            [
                compiler,
                "-fPIC",
                "-shared",
                f"-DCHOICE={choice}",
                "-Wl,-soname,libkdive-choice.so.1",
                "-o",
                str(destination),
                str(source),
            ],
            check=True,
            env={"PATH": os.environ["PATH"], "LC_ALL": "C"},
        )
    main_source = tmp_path / "main.c"
    main_source.write_text(
        "extern int choice(void);\nint main(void) { return choice() == 7 ? 0 : 1; }\n"
    )
    executable = tmp_path / "runpath-probe"
    subprocess.run(
        [
            compiler,
            str(main_source),
            f"-L{unused_dir}",
            "-l:libkdive-choice.so.1",
            "-Wl,-rpath,$ORIGIN/selected",
            "-o",
            str(executable),
        ],
        check=True,
        env={"PATH": os.environ["PATH"], "LC_ALL": "C"},
    )
    return executable, selected.resolve(), unused.resolve()


def _compile_choice_library(destination: Path, choice: int) -> None:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("C compiler is required for the ELF hwcaps fixture")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = destination.parent / f"choice-{choice}.c"
    source.write_text(f"int choice(void) {{ return {choice}; }}\n")
    subprocess.run(
        [
            compiler,
            "-fPIC",
            "-shared",
            "-Wl,-soname,libkdive-choice.so.1",
            "-o",
            str(destination),
            str(source),
        ],
        check=True,
        env={"PATH": os.environ["PATH"], "LC_ALL": "C"},
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_manifest(interpreter: Path) -> dict[str, object]:
    closure, interpreters = bootstrap_elf.runtime_elf_closure(
        [interpreter], required_libraries=("libseccomp.so.2",)
    )
    entries = []
    for path in sorted(closure | interpreters, key=str):
        if path == interpreter.resolve():
            kind = "python-interpreter"
        elif path in interpreters:
            kind = "elf-interpreter"
        else:
            kind = "elf-dependency"
        entries.append({"kind": kind, "path": str(path), "sha256": _digest(path)})
    return {
        "schema_version": 1,
        "architecture": {"amd64": "x86_64"}.get(os.uname().machine, os.uname().machine),
        "interpreter": str(interpreter.resolve()),
        "bootstrap_modules": [
            "kdive",
            "kdive.capture_bootstrap",
            "kdive.jobs",
            "kdive.jobs.capture_operations",
            "kdive.jobs.capture_operations.sandbox",
        ],
        "files": entries,
    }


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o644)


def test_elf_closure_hashes_runtime_runpath_selection_not_competing_soname(
    tmp_path: Path,
) -> None:
    executable, selected, unused = _compile_runpath_fixture(tmp_path)

    closure, _interpreters = bootstrap_elf.runtime_elf_closure([executable])
    hashes = {path: _digest(path) for path in closure}

    assert selected in hashes
    assert hashes[selected] == _digest(selected)
    assert unused not in hashes


def test_runtime_verifier_rejects_new_higher_priority_hwcaps_selection(tmp_path: Path) -> None:
    executable, selected, _unused = _compile_runpath_fixture(tmp_path)
    payload = _fixture_manifest(executable)
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, payload)
    manifest_bytes = manifest.read_bytes()
    manifest_inode = manifest.stat().st_ino
    verify_capture_bootstrap_manifest(manifest, executable, expected_uid=os.getuid())
    assert manifest.read_bytes() == manifest_bytes
    assert manifest.stat().st_ino == manifest_inode
    base_digest = _digest(selected)

    loader = bootstrap_elf.elf_interpreter(executable)
    assert loader is not None
    help_result = subprocess.run(
        [str(loader), "--help"],
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        text=True,
        capture_output=True,
        check=True,
    )
    supported = next(
        (
            line.strip().split()[0]
            for line in help_result.stdout.splitlines()
            if "(supported, searched)" in line
        ),
        None,
    )
    if supported is None:
        pytest.skip("runtime loader exposes no supported glibc-hwcaps directory")
    hwcaps = selected.parent / "glibc-hwcaps" / supported / selected.name
    _compile_choice_library(hwcaps, 9)

    assert _digest(selected) == base_digest
    with pytest.raises(RuntimeError, match="closure drift"):
        verify_capture_bootstrap_manifest(manifest, executable, expected_uid=os.getuid())
    assert manifest.read_bytes() == manifest_bytes
    assert manifest.stat().st_ino == manifest_inode


def test_runtime_verifier_rejects_writable_fingerprinted_file(tmp_path: Path) -> None:
    executable, selected, _unused = _compile_runpath_fixture(tmp_path)
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, _fixture_manifest(executable))
    selected.chmod(0o666)

    with pytest.raises(PermissionError, match="fingerprint file.*group/world writable"):
        verify_capture_bootstrap_manifest(manifest, executable, expected_uid=os.getuid())


def test_runtime_verifier_rejects_replace_capable_ancestor(tmp_path: Path) -> None:
    executable, selected, _unused = _compile_runpath_fixture(tmp_path)
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, _fixture_manifest(executable))
    selected.parent.chmod(0o777)
    metadata = selected.parent.stat()

    with pytest.raises(PermissionError) as error:
        verify_capture_bootstrap_manifest(manifest, executable, expected_uid=os.getuid())

    assert str(error.value) == (
        "capture bootstrap fingerprint ancestor is replaceable: "
        f"path={str(selected.parent)!r} uid={metadata.st_uid} "
        f"gid={metadata.st_gid} mode=0777"
    )


def test_fingerprint_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"approved")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(PermissionError, match="not safely openable"):
        bootstrap_attestation.fingerprint(link, expected_uid=os.getuid())


def test_fingerprint_rejects_path_replaced_after_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "fingerprinted"
    target.write_bytes(b"approved")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement")
    real_open = os.open
    replaced = False

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == target.name and not replaced:
            replaced = True
            os.replace(replacement, target)
        return descriptor

    monkeypatch.setattr(bootstrap_attestation.os, "open", racing_open)

    with pytest.raises(RuntimeError, match="changed during verification"):
        bootstrap_attestation.fingerprint(target, expected_uid=os.getuid())


@pytest.mark.parametrize("mutation", ["missing", "extra", "reordered"])
def test_runtime_verifier_rejects_nonexact_or_reordered_file_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    output = tmp_path / "manifest.json"
    built = _run(
        "build",
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
        "--output",
        str(output),
    )
    assert built.returncode == 0, built.stderr
    payload = json.loads(output.read_text())
    files = payload["files"]
    if mutation == "missing":
        files.remove(next(entry for entry in files if entry["kind"] == "elf-dependency"))
        expected = "closure drift"
    elif mutation == "extra":
        extra = tmp_path / "unused-elf"
        shutil.copy2(sys.executable, extra)
        files.append({"kind": "elf-dependency", "path": str(extra), "sha256": _digest(extra)})
        files.sort(key=lambda entry: entry["path"])
        expected = "closure drift"
    else:
        files.reverse()
        expected = "sorted"
    _write_manifest(output, payload)
    with pytest.raises(RuntimeError, match=expected):
        verify_capture_bootstrap_manifest(output, Path(sys.executable), expected_uid=os.getuid())


@pytest.mark.parametrize(
    "output",
    [
        "libbad.so.1 => ../../attacker/libbad.so.1 (0x1234)\n",
        "libbad.so.1 => not found\n",
        "unexpected loader diagnostic\n",
    ],
)
def test_loader_list_parser_fails_closed_on_untrusted_output(output: str) -> None:
    with pytest.raises(RuntimeError, match="loader trace"):
        bootstrap_elf.parse_loader_list(output)


def test_loader_list_parser_ignores_address_only_virtual_mapping() -> None:
    executable = Path(sys.executable).resolve()
    output = f"(0x1234)\n{executable} (0x5678)\n"

    assert bootstrap_elf.parse_loader_list(output) == {executable}


def test_loader_list_parser_refuses_ambiguity_and_output_overflow(tmp_path: Path) -> None:
    first = tmp_path / "first.so"
    second = tmp_path / "second.so"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    ambiguous = f"libsame.so.1 => {first} (0x1234)\nlibsame.so.1 => {second} (0x5678)\n"

    with pytest.raises(RuntimeError, match="ambiguous"):
        bootstrap_elf.parse_loader_list(ambiguous)
    with pytest.raises(RuntimeError, match="1048576"):
        bootstrap_elf.parse_loader_list("x" * 1_048_577)


def test_builder_records_real_interpreter_arch_and_absolute_dependency_closure(
    tmp_path: Path,
) -> None:
    output = tmp_path / "manifest.json"
    result = _run(
        "build",
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
        "--output",
        str(output),
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text())
    assert manifest["interpreter"] == str(Path(sys.executable).resolve())
    assert manifest["architecture"] in {"x86_64", "ppc64le"}
    assert all(Path(entry["path"]).is_absolute() for entry in manifest["files"])
    assert any(entry["kind"] == "elf-interpreter" for entry in manifest["files"])
    assert any(entry["kind"] == "bootstrap-python" for entry in manifest["files"])
    assert output.read_bytes().endswith(b"\n")


def test_builder_refresh_is_atomic_and_idempotent(tmp_path: Path) -> None:
    output = tmp_path / "manifest.json"
    arguments = (
        "build",
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
        "--output",
        str(output),
    )
    first = _run(*arguments)
    first_inode = output.stat().st_ino
    second = _run(*arguments)

    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "changed"
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "unchanged"
    assert output.stat().st_ino == first_inode


def test_verify_rejects_wrong_interpreter_and_import_trace_drift(tmp_path: Path) -> None:
    output = tmp_path / "manifest.json"
    built = _run(
        "build",
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
        "--output",
        str(output),
    )
    assert built.returncode == 0, built.stderr
    wrong = _run(
        "verify",
        "--manifest",
        str(output),
        "--interpreter",
        "/bin/true",
        "--source-root",
        str(_ROOT / "src"),
    )
    assert wrong.returncode != 0
    assert "interpreter" in wrong.stderr

    payload = json.loads(output.read_text())
    payload["bootstrap_modules"].append("tenant.module")
    output.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    drift = _run(
        "verify",
        "--manifest",
        str(output),
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
    )
    assert drift.returncode != 0
    assert "import trace" in drift.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="unprivileged refusal requires a non-root test uid")
def test_install_refuses_unprivileged_user(tmp_path: Path) -> None:
    staged = tmp_path / "staged.json"
    staged.write_text("{}\n")
    result = _run(
        "install", "--staged", str(staged), "--destination", str(tmp_path / "installed.json")
    )
    assert result.returncode != 0
    assert "root" in result.stderr


def test_docker_and_ansible_generate_after_final_interpreter() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text()
    build = dockerfile.index("build-capture-bootstrap-manifest.py build")
    final = dockerfile.index("FROM python:3.14.6-slim-bookworm", dockerfile.index("AS builder") + 1)
    user = dockerfile.index("USER kdive")
    assert final < build < user
    assert "build-capture-bootstrap-manifest.py install" in dockerfile[build:user]

    tasks = (_ROOT / "deploy/ansible/roles/libvirt_stack/tasks/main.yml").read_text()
    assert "Build capture bootstrap manifest with target-native interpreter" in tasks
    assert "Install root-owned capture bootstrap manifest" in tasks
    assert tasks.index("Build capture bootstrap manifest") < tasks.index(
        "Install root-owned capture bootstrap manifest"
    )


def test_package_initializer_is_inert_under_isolated_python() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import sys, kdive; print('kdive.version' in sys.modules, "
                "'__version__' in kdive.__dict__)"
            ),
        ],
        cwd=_ROOT,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"
