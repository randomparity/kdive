"""Behavioral tests for deploy/remote-libvirt-guest-helpers/kdive-install-kernel's exit codes.

These pin the exit-code contract of ADR-0489 at the artifact that actually emits it. The worker
side (``src/kdive/providers/remote_libvirt/lifecycle/install.py``) maps those codes onto failure
categories, and since ADR-0483 the category decides whether a failed install may be retried — so
the two separately-versioned artifacts have to agree on what ``75`` means. The worker's constant
is imported here rather than restated, so a change on either side breaks this test.

The helper is driven with ``file://`` URLs: ``curl`` fetches a local path with no network and no
object store, which is enough to exercise the fetch step (missing file) and the step right after
it (a file that is not a gzip tarball) — the two conditions the contract has to tell apart.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from kdive.providers.remote_libvirt.lifecycle.install import _HELPER_EX_TEMPFAIL

HELPER = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "remote-libvirt-guest-helpers"
    / "kdive-install-kernel"
)
BASH = shutil.which("bash")

# The helper shells out to curl for the bundle fetch; without it the fetch step cannot be reached.
requires_curl = pytest.mark.skipif(
    shutil.which("curl") is None, reason="curl is required to exercise the helper's fetch step"
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None, "bash is required to run the helper"
    return subprocess.run(
        [BASH, str(HELPER), *args],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def _install_args(url: str) -> list[str]:
    return ["install", "--url", url, "--cmdline", "console=ttyS0", "--method", "host_dump"]


@requires_curl
def test_unfetchable_bundle_exits_tempfail(tmp_path: Path) -> None:
    """The one transient condition: the bundle never arrived, so a retry can heal it."""
    proc = _run(*_install_args(f"file://{tmp_path / 'no-such-bundle.tar.gz'}"))
    assert proc.returncode == _HELPER_EX_TEMPFAIL
    assert "bundle download failed" in proc.stderr


@requires_curl
def test_fetched_but_unextractable_bundle_exits_deterministic(tmp_path: Path) -> None:
    """One step later, the opposite verdict: bytes that arrived whole and will not extract.

    Re-downloading yields the same bytes, so this must NOT be the retryable code — this is the
    discrimination the single-exit-code helper could not express.
    """
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"not a gzip tarball")
    proc = _run(*_install_args(f"file://{bundle}"))
    assert proc.returncode == 1
    assert proc.returncode != _HELPER_EX_TEMPFAIL
    assert "bundle extract failed" in proc.stderr


def test_missing_required_argument_exits_deterministic() -> None:
    """A worker/helper contract mismatch is permanent; no retry supplies the argument."""
    proc = _run("install", "--url", "file:///dev/null")
    assert proc.returncode == 1


def test_unknown_subcommand_exits_deterministic() -> None:
    proc = _run("wat")
    assert proc.returncode == 1
    assert "unknown subcommand" in proc.stderr
