"""Build/install and deployment guards for capture bootstrap attestation (ADR-0558)."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kdive.jobs.capture_operations.bootstrap import bootstrap_attestation, bootstrap_elf
from kdive.jobs.capture_operations.launcher import verify_capture_bootstrap_manifest

_ROOT = Path(__file__).parents[3]
_SCRIPT = _ROOT / "scripts/generate/build-capture-bootstrap-manifest.py"


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
            "kdive.jobs",
            "kdive.jobs.capture_operations",
            "kdive.jobs.capture_operations.bootstrap.bootstrap_entrypoint",
            "kdive.jobs.capture_operations.process.sandbox",
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

    message = str(error.value)
    assert message == (
        "capture bootstrap fingerprint ancestor rejected: "
        "reason=fingerprint_ancestor_replaceable "
        "component=capture_manifest_fingerprint_ancestor "
        f"uid={metadata.st_uid} gid={metadata.st_gid} mode=0777"
    )
    assert str(selected.parent) not in message


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


def test_builder_rejects_replaceable_ancestor_under_relaxed_umask(tmp_path: Path) -> None:
    output = tmp_path / "manifest.json"
    unhardened_source = tmp_path / "src"
    shutil.copytree(_ROOT / "src", unhardened_source)
    unhardened_source.chmod(0o775)
    (unhardened_source / "kdive").chmod(0o775)

    result = _run(
        "build",
        "--interpreter",
        sys.executable,
        "--source-root",
        str(unhardened_source),
        "--output",
        str(output),
    )
    # Manifest build fails closed when ancestor is group/world writable
    assert result.returncode != 0
    assert "fingerprint_ancestor_replaceable" in result.stderr


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


def test_manifest_install_closes_root_producer_worker_consumer_mode_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = tmp_path / "staged.json"
    built = _run(
        "build",
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
        "--output",
        str(staged),
    )
    assert built.returncode == 0, built.stderr

    namespace = runpy.run_path(str(_SCRIPT))
    install = namespace["_install"]
    prepare_parent = namespace["_prepare_install_parent"]
    assert_parent_selected = namespace["_assert_install_parent_selected"]
    script_os = install.__globals__["os"]
    legacy_destination = tmp_path / "legacy" / "kdive" / "capture-bootstrap-manifest.json"
    destination = tmp_path / "fixed" / "kdive" / "capture-bootstrap-manifest.json"
    for trusted_parent in (legacy_destination.parent.parent, destination.parent.parent):
        trusted_parent.mkdir()
        trusted_parent.chmod(0o755)
    monkeypatch.setattr(script_os, "geteuid", lambda: 0)
    monkeypatch.setattr(script_os, "fchown", lambda *_args: None)
    real_stat = Path.stat
    read_regular_at = namespace["_read_regular_at"]

    def current_user_owned_read(parent_fd: int, name: str, **kwargs: object) -> bytes:
        kwargs["owner_uid"] = os.getuid()
        kwargs["group_gid"] = os.getgid()
        return read_regular_at(parent_fd, name, **kwargs)

    monkeypatch.setitem(
        install.__globals__,
        "_read_regular_at",
        current_user_owned_read,
    )
    previous_umask = os.umask(0)
    try:

        def legacy_prepare(path: Path, **_kwargs: object) -> tuple[int, bool]:
            path.mkdir(parents=True, exist_ok=True)
            return (
                os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC),
                False,
            )

        def skip_legacy_parent_retarget_check(*_args: object, **_kwargs: object) -> None:
            return None

        def current_user_prepare(path: Path, **_kwargs: object) -> tuple[int, bool]:
            return prepare_parent(
                path,
                owner_uid=os.getuid(),
                group_gid=os.getgid(),
            )

        def current_user_parent_check(
            path: Path,
            descriptor: int,
            **_kwargs: object,
        ) -> None:
            assert_parent_selected(
                path,
                descriptor,
                owner_uid=os.getuid(),
                group_gid=os.getgid(),
            )

        monkeypatch.setitem(
            install.__globals__,
            "_prepare_install_parent",
            legacy_prepare,
        )
        monkeypatch.setitem(
            install.__globals__,
            "_assert_install_parent_selected",
            skip_legacy_parent_retarget_check,
        )
        install(SimpleNamespace(staged=staged, destination=legacy_destination))
        monkeypatch.setitem(
            install.__globals__,
            "_prepare_install_parent",
            current_user_prepare,
        )
        monkeypatch.setitem(
            install.__globals__,
            "_assert_install_parent_selected",
            current_user_parent_check,
        )
        install(SimpleNamespace(staged=staged, destination=destination))
    finally:
        os.umask(previous_umask)

    legacy_parent = legacy_destination.parent
    assert stat.S_IMODE(real_stat(legacy_parent).st_mode) == 0o777
    producer_verify = _run(
        "verify",
        "--manifest",
        str(legacy_destination),
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
    )
    assert producer_verify.returncode == 0, producer_verify.stderr
    with pytest.raises(PermissionError, match="reason=fingerprint_ancestor_replaceable"):
        verify_capture_bootstrap_manifest(
            legacy_destination, Path(sys.executable), expected_uid=os.getuid()
        )

    assert stat.S_IMODE(real_stat(destination.parent).st_mode) == 0o755
    assert stat.S_IMODE(real_stat(destination.parent.parent).st_mode) == 0o755
    verify_capture_bootstrap_manifest(destination, Path(sys.executable), expected_uid=os.getuid())


def test_manifest_parent_walk_rejects_symlinked_intermediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = runpy.run_path(str(_SCRIPT))
    prepare_parent = namespace["_prepare_install_parent"]
    script_os = prepare_parent.__globals__["os"]
    external = tmp_path / "external"
    external.mkdir()
    intermediate = tmp_path / "SENSITIVE_INTERMEDIATE_SENTINEL"
    intermediate.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(script_os, "fchown", lambda *_args: None)

    with pytest.raises(RuntimeError, match="real directory"):
        prepare_parent(
            intermediate / "nested",
            owner_uid=os.getuid(),
            group_gid=os.getgid(),
        )

    assert list(external.iterdir()) == []


def test_manifest_parent_walk_hardens_replaceable_owned_intermediate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(_SCRIPT))
    prepare_parent = namespace["_prepare_install_parent"]
    script_os = prepare_parent.__globals__["os"]
    replaceable = tmp_path / "replaceable"
    replaceable.mkdir(mode=0o777)
    replaceable.chmod(0o777)
    synced: list[tuple[int, int]] = []
    real_fstat = os.fstat

    def record_fsync(descriptor: int) -> None:
        metadata = real_fstat(descriptor)
        synced.append((metadata.st_dev, metadata.st_ino))

    monkeypatch.setattr(script_os, "fsync", record_fsync)
    descriptor, changed = prepare_parent(
        replaceable / "nested",
        owner_uid=os.getuid(),
        group_gid=os.getgid(),
    )
    os.close(descriptor)

    assert changed is True
    assert stat.S_IMODE(replaceable.stat().st_mode) == 0o755
    assert stat.S_IMODE((replaceable / "nested").stat().st_mode) == 0o755
    assert (replaceable.stat().st_dev, replaceable.stat().st_ino) in synced
    assert (tmp_path.stat().st_dev, tmp_path.stat().st_ino) in synced


@pytest.mark.parametrize("existing_mode", (None, 0o700))
def test_manifest_parent_walk_fsyncs_created_or_hardened_directory_and_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_mode: int | None,
) -> None:
    namespace = runpy.run_path(str(_SCRIPT))
    prepare_parent = namespace["_prepare_install_parent"]
    script_os = prepare_parent.__globals__["os"]
    destination_parent = tmp_path / "install-parent"
    if existing_mode is not None:
        destination_parent.mkdir(mode=existing_mode)
        destination_parent.chmod(existing_mode)
    synced: list[tuple[int, int]] = []
    real_fstat = os.fstat

    def record_fsync(descriptor: int) -> None:
        metadata = real_fstat(descriptor)
        synced.append((metadata.st_dev, metadata.st_ino))

    monkeypatch.setattr(script_os, "fsync", record_fsync)
    descriptor, changed = prepare_parent(
        destination_parent,
        owner_uid=os.getuid(),
        group_gid=os.getgid(),
    )
    os.close(descriptor)

    assert changed is True
    assert (destination_parent.stat().st_dev, destination_parent.stat().st_ino) in synced
    assert (tmp_path.stat().st_dev, tmp_path.stat().st_ino) in synced


@pytest.mark.parametrize("leaf_kind", ("fifo", "symlink"))
def test_manifest_atomic_install_rejects_non_regular_leaf_without_blocking(
    tmp_path: Path, leaf_kind: str
) -> None:
    destination = tmp_path / "manifest.json"
    if leaf_kind == "fifo":
        os.mkfifo(destination)
    else:
        external = tmp_path / "external"
        external.write_text("external", encoding="utf-8")
        destination.symlink_to(external)
    code = """
import os
import runpy
import sys
namespace = runpy.run_path(sys.argv[1])
parent_fd = os.open(sys.argv[2], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
try:
    namespace["_atomic_write_at"](
        parent_fd,
        "manifest.json",
        b"replacement",
        0o644,
        owner_uid=os.getuid(),
        group_gid=os.getgid(),
    )
except RuntimeError as error:
    print(error)
    raise SystemExit(7)
finally:
    os.close(parent_fd)
"""

    result = subprocess.run(
        [sys.executable, "-c", code, str(_SCRIPT), str(tmp_path)],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )

    assert result.returncode == 7
    assert result.stdout == "manifest destination must be a regular file\n"
    assert result.stderr == ""


@pytest.mark.parametrize("failure_step", ("fchmod", "fchown", "write", "fsync", "replace"))
def test_manifest_atomic_install_cleans_temporary_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_step: str,
) -> None:
    namespace = runpy.run_path(str(_SCRIPT))
    atomic_write_at = namespace["_atomic_write_at"]
    script_os = atomic_write_at.__globals__["os"]
    destination = tmp_path / "manifest.json"
    destination.write_bytes(b"authoritative")
    destination.chmod(0o644)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    real_fsync = os.fsync
    real_write = os.write
    write_calls = 0
    parent_syncs = 0

    def injected_failure(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"injected {failure_step}")

    def partial_then_failed_write(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return real_write(descriptor, data[:1])
        raise OSError("injected write")

    def tracked_fsync(descriptor: int) -> None:
        nonlocal parent_syncs
        if failure_step == "fsync" and descriptor != parent_fd:
            raise OSError("injected fsync")
        if descriptor == parent_fd:
            parent_syncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(script_os, "fsync", tracked_fsync)
    if failure_step == "write":
        monkeypatch.setattr(script_os, "write", partial_then_failed_write)
    elif failure_step != "fsync":
        monkeypatch.setattr(script_os, failure_step, injected_failure)
    try:
        with pytest.raises(OSError, match=f"injected {failure_step}"):
            atomic_write_at(
                parent_fd,
                destination.name,
                b"replacement",
                0o644,
                owner_uid=os.getuid(),
                group_gid=os.getgid(),
            )
    finally:
        os.close(parent_fd)

    assert destination.read_bytes() == b"authoritative"
    assert {path.name for path in tmp_path.iterdir()} == {destination.name}
    assert parent_syncs == 1


@pytest.mark.parametrize(
    "staged_kind",
    ("fifo", "symlink", "oversized", "malformed", "wrong_mode"),
)
def test_manifest_install_rejects_untrusted_staged_leaf(tmp_path: Path, staged_kind: str) -> None:
    namespace = runpy.run_path(str(_SCRIPT))
    read_staged = namespace["_read_staged_manifest"]
    maximum = namespace["_MAX_MANIFEST_BYTES"]
    staged = tmp_path / "staged.json"
    if staged_kind == "fifo":
        os.mkfifo(staged)
    elif staged_kind == "symlink":
        external = tmp_path / "external"
        external.write_text('{"schema_version":1}', encoding="utf-8")
        staged.symlink_to(external)
    elif staged_kind == "oversized":
        with staged.open("wb") as output:
            output.seek(maximum)
            output.write(b"\n")
    elif staged_kind == "wrong_mode":
        staged.write_text('{"schema_version":1}\n', encoding="utf-8")
        staged.chmod(0o600)
    else:
        staged.write_text("{", encoding="utf-8")
    if staged_kind != "wrong_mode":
        staged.chmod(0o644)

    with pytest.raises(RuntimeError):
        read_staged(staged)


def test_manifest_install_accepts_root_built_staging_under_sudo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(_SCRIPT))
    read_staged = namespace["_read_staged_manifest"]
    maximum = namespace["_MAX_MANIFEST_BYTES"]
    payload = b'{"schema_version":1}\n'
    expected_uids: list[int] = []

    def root_owned_reader(
        _path: Path,
        *,
        expected_uid: int,
        maximum_size: int,
    ) -> bytes:
        assert maximum_size == maximum
        expected_uids.append(expected_uid)
        if expected_uid != 0:
            raise PermissionError("capture bootstrap manifest has the wrong owner")
        return payload

    monkeypatch.setitem(read_staged.__globals__, "read_manifest", root_owned_reader)

    assert read_staged(tmp_path / "staged.json", expected_uid=1234) == payload
    assert expected_uids == [1234, 0]


def test_manifest_install_rejects_symlinked_staged_intermediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = runpy.run_path(str(_SCRIPT))
    install = namespace["_install"]
    script_os = install.__globals__["os"]
    external = tmp_path / "external"
    external.mkdir()
    (external / "manifest.json").write_text('{"schema_version":1}\n', encoding="utf-8")
    (external / "manifest.json").chmod(0o644)
    staged_intermediate = tmp_path / "staged"
    staged_intermediate.symlink_to(external, target_is_directory=True)
    destination = tmp_path / "destination" / "manifest.json"
    monkeypatch.setattr(script_os, "geteuid", lambda: 0)

    with pytest.raises(RuntimeError, match="staged manifest path is not safely openable"):
        install(
            SimpleNamespace(
                staged=staged_intermediate / "manifest.json",
                destination=destination,
            )
        )

    assert not destination.parent.exists()


@pytest.mark.parametrize("sudo_uid", ("-1", "not-a-uid", "4294967296"))
def test_manifest_install_rejects_invalid_sudo_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sudo_uid: str,
) -> None:
    namespace = runpy.run_path(str(_SCRIPT))
    install = namespace["_install"]
    script_os = install.__globals__["os"]
    staged = tmp_path / "staged.json"
    staged.write_text('{"schema_version":1}\n', encoding="utf-8")
    staged.chmod(0o644)
    destination = tmp_path / "destination" / "manifest.json"
    monkeypatch.setattr(script_os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", sudo_uid)

    with pytest.raises(RuntimeError, match="SUDO_UID"):
        install(SimpleNamespace(staged=staged, destination=destination))

    assert not destination.parent.exists()


def test_manifest_install_keeps_parent_descriptor_across_path_retarget(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(_SCRIPT))
    prepare_parent = namespace["_prepare_install_parent"]
    atomic_write_at = namespace["_atomic_write_at"]
    visible = tmp_path / "visible"
    visible.mkdir()
    parent_fd, _ = prepare_parent(
        visible,
        owner_uid=os.getuid(),
        group_gid=os.getgid(),
    )
    retained = tmp_path / "retained"
    external = tmp_path / "external"
    external.mkdir()
    visible.rename(retained)
    visible.symlink_to(external, target_is_directory=True)
    try:
        atomic_write_at(
            parent_fd,
            "manifest.json",
            b'{"schema_version":1}\n',
            0o644,
            owner_uid=os.getuid(),
            group_gid=os.getgid(),
        )
    finally:
        os.close(parent_fd)

    assert (retained / "manifest.json").is_file()
    assert list(external.iterdir()) == []


def test_manifest_install_rejects_destination_parent_retarget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = runpy.run_path(str(_SCRIPT))
    install = namespace["_install"]
    script_os = install.__globals__["os"]
    real_atomic_write_at = namespace["_atomic_write_at"]
    prepare_parent = namespace["_prepare_install_parent"]
    assert_parent_selected = namespace["_assert_install_parent_selected"]
    read_regular_at = namespace["_read_regular_at"]
    staged = tmp_path / "staged.json"
    staged.write_text('{"schema_version":1}\n', encoding="utf-8")
    staged.chmod(0o644)
    visible = tmp_path / "visible"
    visible.mkdir()
    retained = tmp_path / "retained"
    destination = visible / "manifest.json"

    def current_user_prepare(path: Path, **_kwargs: object) -> tuple[int, bool]:
        return prepare_parent(
            path,
            owner_uid=os.getuid(),
            group_gid=os.getgid(),
        )

    def current_user_parent_check(
        path: Path,
        descriptor: int,
        **_kwargs: object,
    ) -> None:
        assert_parent_selected(
            path,
            descriptor,
            owner_uid=os.getuid(),
            group_gid=os.getgid(),
        )

    def current_user_owned_read(parent_fd: int, name: str, **kwargs: object) -> bytes:
        kwargs["owner_uid"] = os.getuid()
        kwargs["group_gid"] = os.getgid()
        return read_regular_at(parent_fd, name, **kwargs)

    def retarget_after_write(*args: object, **kwargs: object) -> bool:
        changed = real_atomic_write_at(*args, **kwargs)
        visible.rename(retained)
        visible.mkdir()
        return changed

    monkeypatch.setattr(script_os, "geteuid", lambda: 0)
    monkeypatch.setattr(script_os, "fchown", lambda *_args: None)
    monkeypatch.setitem(install.__globals__, "_prepare_install_parent", current_user_prepare)
    monkeypatch.setitem(
        install.__globals__,
        "_assert_install_parent_selected",
        current_user_parent_check,
    )
    monkeypatch.setitem(install.__globals__, "_read_regular_at", current_user_owned_read)
    monkeypatch.setitem(install.__globals__, "_atomic_write_at", retarget_after_write)

    with pytest.raises(RuntimeError, match="destination parent changed during installation"):
        install(SimpleNamespace(staged=staged, destination=destination))

    assert list(visible.iterdir()) == []
    assert (retained / "manifest.json").read_bytes() == staged.read_bytes()


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
    assert (
        "COPY --from=builder /app/scripts/generate/build-capture-bootstrap-manifest.py "
        "/usr/local/libexec/build-capture-bootstrap-manifest.py"
    ) in dockerfile
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
