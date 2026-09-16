# tests/scripts/test_justfile_live_recipes.py
"""Behavioral tests for the live `just` recipes' exit-code handling (#1627, #2517).

Each recipe selects a marker family with pytest, and neither of pytest's own success codes can
tell "the tier passed" from "the tier never ran": exit 0 is also what an all-skip run returns,
and exit 5 ("no tests collected") means the family has zero carriers. A recipe that trusts them
reports a run that proved nothing as green.

`test-live-remote` rejects exit 5 outright (#1627). `test-live-tcg` additionally runs the family's
preflight and requires a real `<N> passed` summary, the gate the hosted spine in
`.github/workflows/live.yml` has enforced since #2048 (#2517).

These drive the real recipes with a stub `uv` on PATH that prints a chosen summary and returns a
chosen exit code, so the assertion is over each recipe's observable behavior rather than over its
source text.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _ROOT / "justfile"
_JUST = shutil.which("just")

# The whole repo is driven through `just` (CI runs `just lint` / `just type` / `just test`), so
# this gate does not fire in CI; it keeps a `just`-less direct-pytest invocation from erroring.
pytestmark = pytest.mark.skipif(_JUST is None, reason="just is required to drive a justfile recipe")


def _run_recipe(
    recipe: str,
    tmp_path: Path,
    uv_exit_code: int,
    uv_stdout: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``recipe`` with a stub ``uv`` that prints ``uv_stdout`` and exits ``uv_exit_code``."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "uv"
    stub.write_text(
        f"#!/usr/bin/env bash\nprintf '%s' {shlex.quote(uv_stdout)}\nexit {uv_exit_code}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    assert _JUST is not None
    return subprocess.run(
        [_JUST, "--justfile", str(_JUSTFILE), "--working-directory", str(_ROOT), recipe],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            **(extra_env or {}),
        },
    )


def _satisfied_tcg_env(tmp_path: Path) -> dict[str, str]:
    """The env `scripts/live-vm/preflight-env.sh tcg` demands, so a test can get past preflight.

    Stubs `qemu-system-ppc64` into the same bin dir `_run_recipe` puts its `uv` stub in, so the
    emulator check passes without the real multi-hundred-megabyte package.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    emulator = stub_dir / "qemu-system-ppc64"
    emulator.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    emulator.chmod(0o755)
    image = tmp_path / "ppc64le.qcow2"
    image.touch()
    kernel_src = tmp_path / "linux"
    kernel_src.mkdir()
    return {
        "KDIVE_STACK_BASE_URL": "http://stack.invalid",
        "KDIVE_OIDC_ISSUER": "http://oidc.invalid",
        "KDIVE_MIGRATION_DATABASE_URL": "postgresql://migration@db.invalid/kdive",
        "KDIVE_SERVER_DATABASE_URL": "postgresql://server@db.invalid/kdive",
        "KDIVE_WORKER_DATABASE_URL": "postgresql://worker@db.invalid/kdive",
        "KDIVE_RECONCILER_DATABASE_URL": "postgresql://reconciler@db.invalid/kdive",
        "KDIVE_S3_ENDPOINT_URL": "http://s3.invalid",
        "KDIVE_S3_BUCKET": "kdive-artifacts",
        "AWS_ACCESS_KEY_ID": "minioadmin",
        "AWS_SECRET_ACCESS_KEY": "minioadmin",  # pragma: allowlist secret - on-box MinIO default
        "KDIVE_GUEST_IMAGE_PPC64LE": str(image),
        "KDIVE_KERNEL_SRC": str(kernel_src),
    }


def test_no_tests_collected_fails_the_remote_recipe(tmp_path: Path) -> None:
    result = _run_recipe("test-live-remote", tmp_path, uv_exit_code=5)
    assert result.returncode != 0, (
        "pytest exit 5 means the live_vm_remote family has zero carriers, so the run proved "
        f"nothing — the recipe must not exit 0 (got {result.returncode}); {result.stdout}"
    )
    assert "live_vm_remote" in result.stderr


def test_passing_remote_run_succeeds(tmp_path: Path) -> None:
    result = _run_recipe("test-live-remote", tmp_path, uv_exit_code=0)
    assert result.returncode == 0, result.stderr


def test_failing_remote_run_propagates_its_exit_code(tmp_path: Path) -> None:
    result = _run_recipe("test-live-remote", tmp_path, uv_exit_code=1)
    assert result.returncode == 1, result.stderr


def test_absent_preflight_env_fails_the_tcg_recipe(tmp_path: Path) -> None:
    """Missing tcg prerequisites must fail loud, not skip through pytest to a green exit."""
    result = _run_recipe("test-live-tcg", tmp_path, uv_exit_code=0)
    assert result.returncode != 0, (
        "the tcg recipe ran with none of the live-stack env its preflight requires, so it proved "
        f"nothing — it must not exit 0 (got {result.returncode}); {result.stdout}"
    )
    assert "KDIVE_STACK_BASE_URL" in result.stderr, result.stderr


def test_all_skipped_tcg_run_fails_naming_the_tier(tmp_path: Path) -> None:
    """The reported defect: `4 skipped, 18584 deselected` is pytest exit 0 and proved nothing."""
    result = _run_recipe(
        "test-live-tcg",
        tmp_path,
        uv_exit_code=0,
        uv_stdout="4 skipped, 18584 deselected in 12.34s\n",
        extra_env=_satisfied_tcg_env(tmp_path),
    )
    assert result.returncode != 0, (
        "every live_vm_tcg proof skipped, so the tier proved nothing — the recipe must not exit 0 "
        f"(got {result.returncode}); {result.stdout}"
    )
    assert "live_vm_tcg" in result.stderr, result.stderr


def test_no_tests_collected_fails_the_tcg_recipe(tmp_path: Path) -> None:
    """Exit 5 is zero-collect: a hard failure here, matching the hosted spine, not a clean skip."""
    result = _run_recipe(
        "test-live-tcg",
        tmp_path,
        uv_exit_code=5,
        uv_stdout="no tests ran in 0.42s\n",
        extra_env=_satisfied_tcg_env(tmp_path),
    )
    assert result.returncode != 0, (
        f"pytest exit 5 means no live_vm_tcg proof ran (got {result.returncode}); {result.stdout}"
    )
    assert "live_vm_tcg" in result.stderr, result.stderr


def test_passing_tcg_run_succeeds(tmp_path: Path) -> None:
    result = _run_recipe(
        "test-live-tcg",
        tmp_path,
        uv_exit_code=0,
        uv_stdout="4 passed in 1834.21s\n",
        extra_env=_satisfied_tcg_env(tmp_path),
    )
    assert result.returncode == 0, result.stderr


def test_failing_tcg_run_propagates_its_exit_code(tmp_path: Path) -> None:
    """Capturing pytest's output through a pipe must not swallow its status (`pipefail`)."""
    result = _run_recipe(
        "test-live-tcg",
        tmp_path,
        uv_exit_code=1,
        uv_stdout="3 passed, 1 failed in 1902.77s\n",
        extra_env=_satisfied_tcg_env(tmp_path),
    )
    assert result.returncode == 1, (
        "one proof passed, so the '<N> passed' gate is satisfied and pytest's own failure status "
        f"must survive the capture pipeline (got {result.returncode}); {result.stdout}"
    )


def _satisfied_native_env(tmp_path: Path) -> dict[str, str]:
    """The env `preflight-env.sh throwaway provisioned debug-stepping` demands (#2540).

    `test-live` selects `live_vm and not live_vm_tcg`, which spans all three native families, so
    the recipe declares all three and the test must satisfy all three to get past preflight.
    Stubs `gdb` and `qemu-img` into the same bin dir `_run_recipe` puts its `uv` stub in, and
    touches the four paths the families `require_path`.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    for tool in ("gdb", "qemu-img"):
        stub = stub_dir / tool
        stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    paths = {}
    for var, name in (
        ("KDIVE_LIVE_VM_ROOTFS", "rootfs.qcow2"),
        ("KDIVE_LIVE_VM_BZIMAGE", "bzImage"),
        ("KDIVE_LIVE_VM_VMLINUX", "vmlinux"),
        ("KDIVE_GUEST_IMAGE", "live-vm-provisioned-rootfs.qcow2"),
    ):
        target = tmp_path / name
        target.touch()
        paths[var] = str(target)
    return {
        **paths,
        "KDIVE_LIBVIRT_URI": "qemu:///session",
        "KDIVE_LIVE_VM_SYSTEM_ID": "01JQ0000000000000000000000",
        "KDIVE_S3_ENDPOINT_URL": "http://s3.invalid",
        "KDIVE_S3_BUCKET": "kdive-artifacts",
    }


def test_absent_preflight_env_fails_the_native_recipe(tmp_path: Path) -> None:
    """Missing native prerequisites must fail loud, not skip through pytest to a green exit."""
    result = _run_recipe("test-live", tmp_path, uv_exit_code=0)
    assert result.returncode != 0, (
        "the native recipe ran with none of the env its preflight requires, so it proved nothing "
        f"— it must not exit 0 (got {result.returncode}); {result.stdout}"
    )
    assert "KDIVE_LIVE_VM_ROOTFS" in result.stderr, result.stderr


def test_all_skipped_native_run_fails_naming_the_tier(tmp_path: Path) -> None:
    """The reported defect (#2540): `3 skipped, 18584 deselected` is exit 0 and proved nothing."""
    result = _run_recipe(
        "test-live",
        tmp_path,
        uv_exit_code=0,
        uv_stdout="3 skipped, 18584 deselected in 9.10s\n",
        extra_env=_satisfied_native_env(tmp_path),
    )
    assert result.returncode != 0, (
        "every native live_vm proof skipped, so the tier proved nothing — the recipe must not "
        f"exit 0 (got {result.returncode}); {result.stdout}"
    )
    assert "native live_vm tier" in result.stderr, result.stderr


def test_no_tests_collected_fails_the_native_recipe(tmp_path: Path) -> None:
    """Exit 5 is zero-collect: a hard failure here, matching the native spine, not a clean skip."""
    result = _run_recipe(
        "test-live",
        tmp_path,
        uv_exit_code=5,
        uv_stdout="no tests ran in 0.31s\n",
        extra_env=_satisfied_native_env(tmp_path),
    )
    assert result.returncode != 0, (
        f"pytest exit 5 means no native live_vm proof ran (got {result.returncode}); "
        f"{result.stdout}"
    )
    assert "native live_vm tier" in result.stderr, result.stderr


def test_passing_native_run_succeeds(tmp_path: Path) -> None:
    result = _run_recipe(
        "test-live",
        tmp_path,
        uv_exit_code=0,
        uv_stdout="3 passed in 812.44s\n",
        extra_env=_satisfied_native_env(tmp_path),
    )
    assert result.returncode == 0, result.stderr


def test_failing_native_run_propagates_its_exit_code(tmp_path: Path) -> None:
    """Capturing pytest's output through a pipe must not swallow its status (`pipefail`)."""
    result = _run_recipe(
        "test-live",
        tmp_path,
        uv_exit_code=1,
        uv_stdout="2 passed, 1 failed in 804.10s\n",
        extra_env=_satisfied_native_env(tmp_path),
    )
    assert result.returncode == 1, (
        "one proof passed, so the '<N> passed' gate is satisfied and pytest's own failure status "
        f"must survive the capture pipeline (got {result.returncode}); {result.stdout}"
    )


def test_no_tests_collected_fails_the_agent_smoke_recipe(tmp_path: Path) -> None:
    """The `rc -eq 5` tolerance this recipe carried reported a zero-collect run as green (#2540).

    The tier has had marked carriers since ADR-0411, so "marked suite absent" no longer justifies
    the branch, and what it covered instead was a run that had silently stopped selecting them.
    """
    result = _run_recipe("test-agent-smoke", tmp_path, uv_exit_code=5, uv_stdout="no tests ran\n")
    assert result.returncode != 0, (
        "pytest collected no agent_smoke test, so the run proved nothing and must not exit 0 "
        f"(got {result.returncode}); {result.stdout}"
    )
    assert "agent_smoke" in result.stderr, result.stderr


def test_passing_agent_smoke_run_succeeds(tmp_path: Path) -> None:
    """Removing the tolerance must not turn a tier that really ran its carriers red."""
    result = _run_recipe(
        "test-agent-smoke",
        tmp_path,
        uv_exit_code=0,
        uv_stdout="2 passed, 18655 deselected in 4.02s\n",
    )
    assert result.returncode == 0, result.stderr
