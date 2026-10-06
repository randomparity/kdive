"""Gated smoke test for the container image (ADR-0088 Phase 2).

Opt-in: set ``KDIVE_IMAGE`` to a built image tag and have ``docker`` on PATH. The
CI ``image-build`` job builds ``kdive:ci`` and runs this; locally,
``KDIVE_IMAGE=kdive:dev uv run pytest tests/image/test_image_smoke.py -q``.

It asserts the image's behaviour at the boundary CI can reach without backends:
the entrypoint lists every subcommand, each app command dispatches past argparse
into ADR-0087 config validation (a configuration_error, not an argparse error),
and the worker toolchain resolves on PATH for the non-root user.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KDIVE_IMAGE") is None or shutil.which("docker") is None,
    reason="set KDIVE_IMAGE and have docker to run the image smoke test",
)

_COMMANDS = ("server", "worker", "reconciler", "migrate")

# The kernel-build toolchain the worker's Build plane invokes (ADR-0146): a Linux `make`
# hard-requires flex/bison/bc, and the warm-tree lane also shells out to git (patch_ref +
# clone), rsync (warm-tree mirror), and xz. Each resolves on PATH and answers `--version`.
_BUILD_TOOLS = ("flex", "bison", "bc", "git", "rsync", "xz")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "run", "--rm", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _image() -> str:
    img = os.environ.get("KDIVE_IMAGE")
    assert img is not None  # narrowed for the type checker; skipif guards the None case
    return img


def test_entrypoint_lists_subcommands() -> None:
    res = _run(_image(), "--help")
    assert res.returncode == 0, res.stderr
    for cmd in _COMMANDS:
        assert cmd in res.stdout


def test_each_command_dispatches_past_argparse() -> None:
    # With no KDIVE_* config the command must reach ADR-0087 validation and fail
    # with a configuration error — proving it dispatched, not that argparse rejected
    # an unknown subcommand (which would exit 2 with a "invalid choice" usage error).
    img = _image()
    for cmd in _COMMANDS:
        res = _run(img, cmd)
        assert res.returncode != 0, f"{cmd} unexpectedly succeeded without config"
        combined = res.stdout + res.stderr
        assert "configuration" in combined.lower(), f"{cmd}: not a config failure: {combined}"
        assert "invalid choice" not in combined, f"{cmd}: argparse rejected the subcommand"


def test_version_reports_baked_provenance() -> None:
    # ADR-0370: CI builds this image with KDIVE_COMMIT + KDIVE_RELEASE=false, so the running
    # binary must self-report a dev build with a baked 12+ hex commit — proving _buildinfo.py
    # survived the multi-stage COPY and the PYTHONPATH import, not just that the wiring exists.
    # {12,} tolerates git extending the abbreviation on a (astronomically unlikely) collision.
    res = _run(_image(), "--version")
    assert res.returncode == 0, res.stderr
    assert re.match(r"^kdive \d+\.\d+\.\d+-dev\+g[0-9a-f]{12,}$", res.stdout.strip()), res.stdout


def test_worker_toolchain_on_path() -> None:
    img = _image()
    for tool in ("drgn", "gdb", "virsh"):
        res = _run("--entrypoint", tool, img, "--version")
        assert res.returncode == 0, f"{tool} missing: {res.stderr}"


def test_kernel_build_toolchain_on_path() -> None:
    # ADR-0146: the Build plane's `make` lane needs the full kernel-build toolchain in the
    # image. Without it the warm-tree/server build lane cannot compile on any shipped image.
    img = _image()
    for tool in _BUILD_TOOLS:
        res = _run("--entrypoint", tool, img, "--version")
        assert res.returncode == 0, f"{tool} missing: {res.stderr}"


def test_runtime_source_permissions_and_bytes() -> None:
    script = """
import hashlib, json
from pathlib import Path
root = Path('/app/src')
paths = [root, *root.rglob('*'),
         Path('/usr/local/libexec/build-capture-bootstrap-manifest.py')]
records = {}
for path in paths:
    if '__pycache__' in path.parts or path.name == '_buildinfo.py':
        continue
    metadata = path.stat()
    records[str(path)] = [metadata.st_uid, metadata.st_mode & 0o777,
                         path.is_dir(),
                         None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()]
print(json.dumps(records))
"""
    result = _run("--entrypoint", "python", _image(), "-c", script)
    assert result.returncode == 0, result.stderr
    records = json.loads(result.stdout)
    root = Path(__file__).resolve().parents[2]
    expected = {"/app/src", "/usr/local/libexec/build-capture-bootstrap-manifest.py"}
    expected.update(
        f"/app/{path.relative_to(root)}"
        for path in (root / "src").rglob("*")
        if "__pycache__" not in path.parts and path.name != "_buildinfo.py"
    )
    assert set(records) == expected
    for name, (uid, mode, is_dir, digest) in records.items():
        assert uid == 0, name
        assert mode & 0o022 == 0, name
        required = 0o555 if is_dir else 0o444
        assert mode & required == required, name
        local = (
            root / "scripts/generate/build-capture-bootstrap-manifest.py"
            if name.startswith("/usr/local/libexec/")
            else root / name.removeprefix("/app/")
        )
        if not is_dir:
            assert digest == hashlib.sha256(local.read_bytes()).hexdigest(), name
            executable = local.stat().st_mode & 0o111
            assert mode & executable == executable, name
