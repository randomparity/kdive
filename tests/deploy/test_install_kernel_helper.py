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
import tarfile
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


def _run(
    *args: str, path: str = "/usr/bin:/bin", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None, "bash is required to run the helper"
    return subprocess.run(
        [BASH, str(HELPER), *args],
        env={"PATH": path, **(env or {})},
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
    assert "bundle extract failed" in proc.stderr


def test_absent_curl_exits_deterministic_not_tempfail(tmp_path: Path) -> None:
    """A guest image with no curl is an image defect, and no number of retries installs one.

    Without the preflight, `curl … || die_tempfail` swallows the shell's 127 for a program it
    could not find and reports a permanent image defect as the retryable code — which would make
    the most-documented setup mistake in this repo burn the whole retry budget.
    """
    empty_path = tmp_path / "bin"
    empty_path.mkdir()
    proc = _run(*_install_args("file:///dev/null"), path=str(empty_path))
    assert proc.returncode == 1
    assert "curl is not installed" in proc.stderr


@requires_curl
def test_refused_connection_exits_tempfail() -> None:
    """curl ran and could not reach the store: the transient this contract exists to name.

    Port 1 on loopback refuses instantly, and curl does not retry a refused connection without
    `--retry-connrefused`, so this costs no wall time.
    """
    proc = _run(*_install_args("http://127.0.0.1:1/bundle.tar.gz"))
    assert proc.returncode == _HELPER_EX_TEMPFAIL
    assert "bundle download failed" in proc.stderr


def test_missing_required_argument_exits_deterministic() -> None:
    """A worker/helper contract mismatch is permanent; no retry supplies the argument."""
    proc = _run("install", "--url", "file:///dev/null")
    assert proc.returncode == 1


def test_unknown_subcommand_exits_deterministic() -> None:
    proc = _run("wat")
    assert proc.returncode == 1
    assert "unknown subcommand" in proc.stderr


# The base image's default BLS options, as `grubby --copy-default` hands them to the kdive slot. The
# crashkernel= range is what a Rocky 10 image ships (#3094).
_DEFAULT_ARGS = "ro root=UUID=0000 crashkernel=1G-4G:192M,4G-64G:256M,64G-:512M rhgb"
_KVER = "6.18.54-kdive-test"

# A grubby that models the one slot the helper writes: --copy-default prepends the default's
# options, --update-kernel=TITLE=kdive --remove-args=<key> drops <key> and <key>=*. Any other
# --update-kernel target is not the slot. Every call is logged.
_GRUBBY_STUB = """#!/bin/bash
state="$KDIVE_TEST_STATE"
echo "$*" >>"$state/grubby.log"
case "$1" in
--info=ALL) exit 0 ;;
--add-kernel=*)
  args="" copy=0
  for a in "$@"; do
    case "$a" in --args=*) args="${a#--args=}" ;; --copy-default) copy=1 ;; esac
  done
  [ "$copy" = 1 ] && args="$(cat "$state/default_args") $args"
  echo "$args" >"$state/slot_args"
  ;;
--update-kernel=TITLE=kdive)
  [ -z "${KDIVE_TEST_FAIL_UPDATE:-}" ] || exit 1
  key="${2#--remove-args=}" kept=()
  for tok in $(cat "$state/slot_args"); do
    case "$tok" in "$key" | "$key"=*) ;; *) kept+=("$tok") ;; esac
  done
  echo "${kept[*]}" >"$state/slot_args"
  ;;
*) exit 1 ;;
esac
"""


def _stub_guest(tmp_path: Path) -> tuple[str, dict[str, str]]:
    """Stub every host-mutating command the install path runs; return its PATH and env.

    `rm` passes through only inside the test's TMPDIR, so the helper's own scratch cleanup still
    runs while its `rm -rf /lib/modules/<ver>` never touches the host.
    """
    real_rm = shutil.which("rm")
    assert real_rm is not None
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "default_args").write_text(_DEFAULT_ARGS + "\n")
    bodies = {
        "grubby": _GRUBBY_STUB,
        "systemctl": '#!/bin/bash\necho "$*" >>"$KDIVE_TEST_STATE/systemctl.log"\n',
        "rm": f'#!/bin/bash\ncase "${{@: -1}}" in "$TMPDIR"/*) exec {real_rm} "$@" ;; esac\n',
    }
    for name in ("dracut", "depmod", "install", "cp"):
        bodies[name] = "#!/bin/bash\nexit 0\n"
    for name, body in bodies.items():
        stub = stubs / name
        stub.write_text(body)
        stub.chmod(0o755)
    env = {"KDIVE_TEST_STATE": str(state), "TMPDIR": str(scratch)}
    return f"{stubs}:/usr/bin:/bin", env


def _bundle(tmp_path: Path) -> str:
    src = tmp_path / "bundle-src"
    (src / "boot").mkdir(parents=True)
    (src / "boot" / "vmlinuz").write_bytes(b"kernel")
    (src / "lib" / "modules" / _KVER).mkdir(parents=True)
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(src / "boot", arcname="boot")
        tar.add(src / "lib", arcname="lib")
    return f"file://{bundle}"


def _install(tmp_path: Path, cmdline: str, method: str, **extra_env: str) -> tuple[int, str]:
    path, env = _stub_guest(tmp_path)
    url = _bundle(tmp_path)
    args = ["install", "--url", url, "--cmdline", cmdline, "--method", method]
    proc = _run(*args, path=path, env={**env, **extra_env})
    return proc.returncode, proc.stderr


def _state(tmp_path: Path, name: str) -> str:
    log = tmp_path / "state" / name
    return log.read_text() if log.exists() else ""


@requires_curl
def test_non_kdump_install_drops_the_inherited_crashkernel(tmp_path: Path) -> None:
    """A gdbstub slot must not keep the image default's reservation (#3094).

    With it, the guest reserves crash memory, and boot()'s #1610 gate waits for a kdump that a
    non-kdump Run never arms. The rest of the default (root=) and the requested args survive.
    """
    rc, err = _install(tmp_path, "console=ttyS0 nokaslr", "gdbstub")
    assert rc == 0, err
    slot = _state(tmp_path, "slot_args").split()
    assert not [tok for tok in slot if "crashkernel=" in tok]
    assert {"root=UUID=0000", "console=ttyS0", "nokaslr"} <= set(slot)


@requires_curl
def test_kdump_install_keeps_its_requested_crashkernel(tmp_path: Path) -> None:
    """A kdump install runs as before: no removal, its reservation stays, kdump is enabled."""
    rc, err = _install(tmp_path, "console=ttyS0 crashkernel=256M", "kdump")
    assert rc == 0, err
    assert "crashkernel=256M" in _state(tmp_path, "slot_args").split()
    assert "--remove-args" not in _state(tmp_path, "grubby.log")
    assert "enable kdump.service" in _state(tmp_path, "systemctl.log")


@requires_curl
def test_failed_crashkernel_removal_exits_deterministic(tmp_path: Path) -> None:
    """A grubby that cannot edit the slot is an image defect like every other grubby step."""
    rc, err = _install(tmp_path, "console=ttyS0 nokaslr", "gdbstub", KDIVE_TEST_FAIL_UPDATE="1")
    assert rc == 1
    assert "crashkernel" in err
