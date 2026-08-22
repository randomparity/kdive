"""Executable proofs for the pre-import systemd worker release gate."""

from __future__ import annotations

import ast
import os
import runpy
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from kdive.processes.lifecycle.worker_incarnation import worker_incarnation_credential

GATE = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "bin" / "kdive-live-worker-gate"
LIFECYCLE_WRAPPER = GATE.with_name("kdive-live-worker-lifecycle")

_GENERATION = "a" * 32
_INVOCATION = "b" * 32
_INCARNATION = f"local-systemd:kdive-live-worker@1.service:{_GENERATION}"


def _write_fake_python(path: Path, capture: Path) -> tuple[Path, Path]:
    arguments = capture.with_suffix(".args")
    environment = capture.with_suffix(".env")
    path.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$0" "$@" > {shlex.quote(str(arguments))}\n'
        f"/usr/bin/env -0 > {shlex.quote(str(environment))}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return arguments, environment


def _gate_env(tmp_path: Path, python: Path) -> tuple[dict[str, str], Path]:
    root = tmp_path / "state"
    slot = root / "slots/1"
    slot.mkdir(parents=True)
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    credential = credentials / "worker-incarnation"
    credential.write_text("systemd-secret\n", encoding="utf-8")
    env = {
        "AMBIENT_SECRET": "must-not-cross-exec",  # pragma: allowlist secret
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "secret-key",  # pragma: allowlist secret
        "CREDENTIALS_DIRECTORY": str(credentials),
        "INVOCATION_ID": _INVOCATION,
        "KDIVE_BUILD_COMPONENT_ROOTS": "/components",
        "KDIVE_BUILD_USER": "builder",
        "KDIVE_BUILD_WORKSPACE": "/build",
        "KDIVE_DATABASE_URL": "postgresql://worker-member/db",
        "KDIVE_FIXTURE_CATALOG_PATH": "/fixtures/catalog.json",
        "KDIVE_HEALTH_BIND_ADDR": "127.0.0.1:9101",
        "KDIVE_INSTALL_STAGING": "/install",
        "KDIVE_KERNEL_SRC": "/checkout/linux",
        "KDIVE_LIVE_WORKER_STATE_ROOT": str(root),
        "KDIVE_LIBVIRT_URI": "qemu+unix:///session?socket=/run/libvirt.sock",
        "KDIVE_LOG_LEVEL": "INFO",
        "KDIVE_ROOTFS_DIR": "/rootfs",
        "KDIVE_S3_BUCKET": "kdive",
        "KDIVE_S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "KDIVE_S3_REGION": "us-east-1",
        "KDIVE_WORKER_ACCEPTED_LANES": "default,state-fenced",
        "KDIVE_WORKER_INCARNATION_ID": _INCARNATION,
        "KDIVE_WORKER_INCARNATION_KIND": "local",
        "KDIVE_WORKER_PYTHON": str(python),
        "KDIVE_WORKER_SOURCE_ROOT": "/checkout",
    }
    return env, slot


def _run_gate(env: dict[str, str], *, timeout: float = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "1"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def test_gate_waits_while_release_marker_is_absent(tmp_path: Path) -> None:
    python = tmp_path / "fake-python"
    _write_fake_python(python, tmp_path / "capture.json")
    env, _slot = _gate_env(tmp_path, python)
    process = subprocess.Popen(
        [sys.executable, str(GATE), "1"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.05)
        assert process.poll() is None
    finally:
        process.terminate()
        _stdout, stderr = process.communicate(timeout=5)
    assert stderr == ""


@pytest.mark.parametrize(
    "marker",
    [
        "malformed\n",
        f"{'c' * 32}\n{_INVOCATION}\n",
        f"{_GENERATION}\n{'d' * 32}\n",
        f"{_GENERATION}\n{_INVOCATION}\nextra\n",
    ],
)
def test_gate_refuses_malformed_or_stale_release_marker(
    tmp_path: Path,
    marker: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    python = tmp_path / "fake-python"
    capture = tmp_path / "capture.json"
    _write_fake_python(python, capture)
    env, slot = _gate_env(tmp_path, python)
    (slot / "release").write_text(marker, encoding="ascii")

    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(sys, "argv", [str(GATE), "1"])
    _patch_root_owned_files(monkeypatch)
    monkeypatch.setattr(os, "execve", lambda *_args: pytest.fail("stale marker reached exec"))
    namespace = runpy.run_path(str(GATE), run_name="kdive_gate_test")

    with pytest.raises(SystemExit):
        namespace["main"]()
    stderr = capsys.readouterr().err
    assert "slot 1" in stderr
    assert _GENERATION not in stderr
    assert _INVOCATION not in stderr
    assert not capture.exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_gate_rejects_non_regular_release_marker_without_blocking(
    tmp_path: Path, kind: str
) -> None:
    python = tmp_path / "fake-python"
    _write_fake_python(python, tmp_path / "capture.json")
    env, slot = _gate_env(tmp_path, python)
    release = slot / "release"
    if kind == "symlink":
        target = slot / "target"
        target.write_text(f"{_GENERATION}\n{_INVOCATION}\n", encoding="ascii")
        release.symlink_to(target)
    else:
        os.mkfifo(release)

    started = time.monotonic()
    result = _run_gate(env)

    assert time.monotonic() - started < 1.0
    assert result.returncode != 0
    assert "release marker invariant" in result.stderr


def test_gate_execs_exact_worker_with_allowlisted_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "fake-python"
    capture = tmp_path / "capture.json"
    _write_fake_python(python, capture)
    env, slot = _gate_env(tmp_path, python)
    (slot / "release").write_text(f"{_GENERATION}\n{_INVOCATION}\n", encoding="ascii")
    captured: dict[str, object] = {}

    class ExecCalled(Exception):
        pass

    def capture_exec(path: str, arguments: list[str], environment: dict[str, str]) -> None:
        captured.update(path=path, arguments=arguments, environment=environment)
        raise ExecCalled

    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(os, "execve", capture_exec)
    _patch_root_owned_files(monkeypatch)
    monkeypatch.setattr(sys, "argv", [str(GATE), "1"])
    namespace = runpy.run_path(str(GATE), run_name="kdive_gate_test")

    with pytest.raises(ExecCalled):
        namespace["main"]()

    assert captured["path"] == str(python)
    assert captured["arguments"] == [str(python), "-m", "kdive", "worker"]
    assert captured["environment"] == {
        "AWS_ACCESS_KEY_ID": env["AWS_ACCESS_KEY_ID"],
        "AWS_SECRET_ACCESS_KEY": env["AWS_SECRET_ACCESS_KEY"],
        "CREDENTIALS_DIRECTORY": env["CREDENTIALS_DIRECTORY"],
        "KDIVE_BUILD_COMPONENT_ROOTS": env["KDIVE_BUILD_COMPONENT_ROOTS"],
        "KDIVE_BUILD_USER": env["KDIVE_BUILD_USER"],
        "KDIVE_BUILD_WORKSPACE": env["KDIVE_BUILD_WORKSPACE"],
        "KDIVE_DATABASE_URL": env["KDIVE_DATABASE_URL"],
        "KDIVE_FIXTURE_CATALOG_PATH": env["KDIVE_FIXTURE_CATALOG_PATH"],
        "KDIVE_HEALTH_BIND_ADDR": env["KDIVE_HEALTH_BIND_ADDR"],
        "KDIVE_INSTALL_STAGING": env["KDIVE_INSTALL_STAGING"],
        "KDIVE_KERNEL_SRC": env["KDIVE_KERNEL_SRC"],
        "KDIVE_LIBVIRT_URI": env["KDIVE_LIBVIRT_URI"],
        "KDIVE_LOG_LEVEL": "INFO",
        "KDIVE_ROOTFS_DIR": env["KDIVE_ROOTFS_DIR"],
        "KDIVE_S3_BUCKET": env["KDIVE_S3_BUCKET"],
        "KDIVE_S3_ENDPOINT_URL": env["KDIVE_S3_ENDPOINT_URL"],
        "KDIVE_S3_REGION": env["KDIVE_S3_REGION"],
        "KDIVE_WORKER_ACCEPTED_LANES": env["KDIVE_WORKER_ACCEPTED_LANES"],
        "KDIVE_WORKER_INCARNATION_ID": _INCARNATION,
        "KDIVE_WORKER_INCARNATION_KIND": "local",
        "KDIVE_WORKER_SOURCE_ROOT": env["KDIVE_WORKER_SOURCE_ROOT"],
    }
    monkeypatch.setattr(os, "environ", captured["environment"])
    environment = cast(dict[str, str], captured["environment"])
    credential = Path(environment["CREDENTIALS_DIRECTORY"]) / "worker-incarnation"
    assert worker_incarnation_credential(credential).get_secret_value() == "systemd-secret"


@pytest.mark.parametrize("credential_body", [b"", b" \n"])
def test_gate_refuses_empty_systemd_credential(tmp_path: Path, credential_body: bytes) -> None:
    python = tmp_path / "fake-python"
    _write_fake_python(python, tmp_path / "capture.json")
    env, slot = _gate_env(tmp_path, python)
    credential = Path(env["CREDENTIALS_DIRECTORY"]) / "worker-incarnation"
    credential.write_bytes(credential_body)
    (slot / "release").write_text(f"{_GENERATION}\n{_INVOCATION}\n", encoding="ascii")

    result = _run_gate(env)

    assert result.returncode != 0
    assert "slot 1" in result.stderr
    assert "credential" in result.stderr


def test_gate_has_no_application_import_before_exec() -> None:
    source = GATE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for name in (
            [alias.name.partition(".")[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").partition(".")[0]]
        )
    }

    assert "import kdive" not in source
    assert "from kdive" not in source
    assert imports <= sys.stdlib_module_names
    assert "os.execve" in source
    assert GATE.stat().st_mode & 0o111


def test_root_lifecycle_wrapper_execs_only_the_installed_witness() -> None:
    source = LIFECYCLE_WRAPPER.read_text(encoding="utf-8")

    assert source.startswith("#!/bin/bash\nset -euo pipefail\n")
    assert "exec /opt/kdive-live-worker-lifecycle/.venv/bin/python" in source
    assert "-m kdive.processes.lifecycle.systemd_worker_control serve" in source
    assert LIFECYCLE_WRAPPER.stat().st_mode & 0o111


def _patch_root_owned_files(monkeypatch: pytest.MonkeyPatch) -> None:
    real_fstat = os.fstat

    def root_owned_fstat(descriptor: int):
        metadata = real_fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return metadata
        return os.stat_result(
            (
                metadata.st_mode,
                metadata.st_ino,
                metadata.st_dev,
                metadata.st_nlink,
                0,
                metadata.st_gid,
                metadata.st_size,
                metadata.st_atime,
                metadata.st_mtime,
                metadata.st_ctime,
            )
        )

    monkeypatch.setattr(os, "fstat", root_owned_fstat)
