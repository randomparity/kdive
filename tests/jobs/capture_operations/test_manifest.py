"""Build/install and deployment guards for capture bootstrap attestation (ADR-0558)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parents[3]
_SCRIPT = _ROOT / "scripts/build-capture-bootstrap-manifest.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args], text=True, capture_output=True, check=False
    )


def _builder_module() -> Any:
    spec = importlib.util.spec_from_file_location("capture_manifest_builder", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_elf_closure_hashes_runtime_runpath_selection_not_competing_soname(
    tmp_path: Path,
) -> None:
    builder = _builder_module()
    executable, selected, unused = _compile_runpath_fixture(tmp_path)

    closure, _interpreters = builder._elf_closure([executable])
    hashes = {path: builder._sha256(path) for path in closure}

    assert selected in hashes
    assert hashes[selected] == builder._sha256(selected)
    assert unused not in hashes


@pytest.mark.parametrize(
    "output",
    [
        "libbad.so.1 => ../../attacker/libbad.so.1 (0x1234)\n",
        "libbad.so.1 => not found\n",
        "unexpected loader diagnostic\n",
    ],
)
def test_loader_list_parser_fails_closed_on_untrusted_output(output: str) -> None:
    builder = _builder_module()

    with pytest.raises(RuntimeError, match="loader trace"):
        builder._parse_loader_list(output)


def test_loader_list_parser_refuses_ambiguity_and_output_overflow(tmp_path: Path) -> None:
    builder = _builder_module()
    first = tmp_path / "first.so"
    second = tmp_path / "second.so"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    ambiguous = f"libsame.so.1 => {first} (0x1234)\nlibsame.so.1 => {second} (0x5678)\n"

    with pytest.raises(RuntimeError, match="ambiguous"):
        builder._parse_loader_list(ambiguous)
    with pytest.raises(RuntimeError, match="1048576"):
        builder._parse_loader_list("x" * 1_048_577)


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
