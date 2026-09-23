import ast
import json
import os
import re
import resource
import shutil
import socket
import subprocess
import sys
import time
import venv
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from kdive.config.core_settings import _DEFAULT_ACCEPTED_LANES
from kdive.config.external_env import EXTERNAL_ENV_VARS

ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE = ROOT / "scripts" / "live-stack" / "worker-lifecycle.sh"
LIBVIRT_URI = ROOT / "scripts" / "live-stack" / "libvirt-uri.sh"
WORKER_FROM_CHECKOUT = ROOT / "scripts" / "live-stack" / "worker-from-checkout"


def test_worker_from_checkout_uses_provisioned_python_and_checkout_source(tmp_path: Path) -> None:
    fake_python = tmp_path / "python"
    argv = tmp_path / "argv"
    environment = tmp_path / "environment"
    fake_python.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv}\nprintf '%s' \"$PYTHONPATH\" > {environment}\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    launcher = tmp_path / "worker-from-checkout"
    launcher.write_text(
        WORKER_FROM_CHECKOUT.read_text()
        .replace("/opt/kdive-live-worker-lifecycle/.venv/bin/python", str(fake_python))
        .replace(
            'repo_root="$(cd -- "${here}/../.." && pwd)"',
            f'repo_root="{ROOT}"',
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    result = subprocess.run(
        [str(launcher), "-m", "kdive", "worker"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": "/ambient"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert argv.read_text().splitlines() == ["-m", "kdive", "worker"]
    assert environment.read_text().split(":", 1) == [str(ROOT / "src"), "/ambient"]


def test_lifecycle_compatibility_probe_is_isolated_and_precedes_request() -> None:
    lifecycle = LIFECYCLE.read_text()

    assert '"$py" -I' in lifecycle
    assert '"$WORKER_PYTHON" -I' in lifecycle
    assert lifecycle.index("require_compatible_lifecycle") < lifecycle.index("request_path")
    assert "LIFECYCLE_REVISION" not in lifecycle
    assert 'WORKER_EXECUTABLE="${here}/worker-from-checkout"' in lifecycle


def _installed_protocol_python(tmp_path: Path, identity: str) -> Path:
    environment = tmp_path / "installed"
    venv.EnvBuilder(with_pip=False).create(environment)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = environment / "lib" / version / "site-packages"
    (site_packages / "dependencies.pth").write_text(
        f"{Path(pytest.__file__).parent.parent}\n", encoding="utf-8"
    )
    contract = site_packages / "kdive/processes/lifecycle/systemd/systemd_worker_contract.py"
    contract.parent.mkdir(parents=True)
    for package in (contract.parents[2], contract.parent.parent, contract.parent):
        (package / "__init__.py").touch()
    contract.write_text(
        f"def lifecycle_protocol_identity():\n    return {identity!r}\n", encoding="utf-8"
    )
    return environment / "bin/python"


def _copied_lifecycle(tmp_path: Path, installed_python: Path) -> Path:
    script_dir = tmp_path / "scripts/live-stack"
    script_dir.mkdir(parents=True)
    for name in ("lib.sh", "env.sh", "libvirt-uri.sh"):
        (script_dir / name).write_text(
            (ROOT / "scripts/live-stack" / name).read_text(), encoding="utf-8"
        )
    lifecycle = script_dir / "worker-lifecycle.sh"
    lifecycle.write_text(
        LIFECYCLE.read_text()
        .replace("/opt/kdive-live-worker-lifecycle/.venv/bin/python", str(installed_python))
        .replace(
            'source "${here}/env.sh"',
            f'source "${{here}}/env.sh"\nrepo_root="{ROOT}"\npy="{sys.executable}"',
        ),
        encoding="utf-8",
    )
    return lifecycle


@pytest.mark.parametrize("operation", ("start 1", "status", "stop", "recover"))
def test_lifecycle_protocol_mismatch_fails_before_mutation(tmp_path: Path, operation: str) -> None:
    installed_python = _installed_protocol_python(tmp_path, "0:incompatible")
    lifecycle = _copied_lifecycle(tmp_path, installed_python)
    marker = tmp_path / "start-prerequisites"

    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "$1"\n'
            f'require_start_prerequisites() {{ touch "{marker}"; }}\n'
            f"request {operation}",
            "bash",
            str(lifecycle),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "KDIVE_PYTHON": sys.executable,
            "PYTHONPATH": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stderr == (
        "installed lifecycle protocol does not match this checkout; reprovision the runner\n"
    )
    assert not marker.exists(), "a mismatch must stop before any mutation prerequisite or request"


def test_lifecycle_compatibility_command_succeeds_without_a_lifecycle_request(
    tmp_path: Path,
) -> None:
    from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
        lifecycle_protocol_identity,
    )

    installed_python = _installed_protocol_python(tmp_path, lifecycle_protocol_identity())
    lifecycle = _copied_lifecycle(tmp_path, installed_python)

    result = subprocess.run(
        ["bash", str(lifecycle), "compatibility"],
        cwd=tmp_path,
        env={**os.environ, "KDIVE_PYTHON": sys.executable, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_lifecycle_matching_protocol_ignores_shadows_and_reaches_request(tmp_path: Path) -> None:
    from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
        lifecycle_protocol_identity,
    )

    installed_python = _installed_protocol_python(tmp_path, lifecycle_protocol_identity())
    lifecycle = _copied_lifecycle(tmp_path, installed_python)
    shadow = tmp_path / "shadow/kdive/processes/lifecycle/systemd/systemd_worker_contract.py"
    shadow.parent.mkdir(parents=True)
    for package in (shadow.parents[2], shadow.parent.parent, shadow.parent):
        (package / "__init__.py").touch()
    shadow.write_text(
        "def lifecycle_protocol_identity():\n    return '0:shadow'\n", encoding="utf-8"
    )
    request_probe = tmp_path / "request-probe"
    (tmp_path / "shadow/sitecustomize.py").write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from kdive.processes.lifecycle.systemd import systemd_worker_control as control\n"
        "from kdive.processes.lifecycle.systemd.systemd_worker_contract import LifecycleResponse\n"
        "def request_path(path, request):\n"
        "    open(os.environ['LIFECYCLE_REQUEST_PROBE'], 'w').write(request.operation)\n"
        "    return LifecycleResponse(ok=True, code='ok', message='ok', retry_action='none')\n"
        "control.request_path = request_path\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(lifecycle), "status"],
        cwd=tmp_path,
        env={
            **os.environ,
            "KDIVE_PYTHON": sys.executable,
            "LIFECYCLE_REQUEST_PROBE": str(request_probe),
            "PYTHONPATH": str(tmp_path / "shadow"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert request_probe.read_text() == "status"


def _lifecycle_status(
    tmp_path: Path, response: str, *, expected_slots: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run the real wrapper against a Python import-time lifecycle response stub."""
    from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
        lifecycle_protocol_identity,
    )

    installed_python = _installed_protocol_python(tmp_path, lifecycle_protocol_identity())
    lifecycle = _copied_lifecycle(tmp_path, installed_python)
    (tmp_path / "sitecustomize.py").write_text(
        "import os\n"
        "from kdive.processes.lifecycle.systemd import systemd_worker_control as control\n"
        "from kdive.processes.lifecycle.systemd.systemd_worker_contract import LifecycleResponse\n"
        "def request_path(path, request):\n"
        "    values = [os.environ.get(name, '<missing>') for name in (\n"
        "        'KDIVE_DATABASE_URL', 'KDIVE_MIGRATION_DATABASE_URL',\n"
        "        'KDIVE_SERVER_DATABASE_URL', 'KDIVE_RECONCILER_DATABASE_URL',\n"
        "        'KDIVE_WORKER_DATABASE_URL')]\n"
        "    with open(os.environ['KDIVE_ENV_PROBE'], 'w') as probe:\n"
        "        probe.write('\\n'.join(values))\n"
        "    return LifecycleResponse.model_validate_json(os.environ['KDIVE_RESPONSE'].encode())\n"
        "control.request_path = request_path\n",
        encoding="utf-8",
    )
    probe = tmp_path / "environment"
    return subprocess.run(
        ["bash", str(lifecycle), "status"],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(tmp_path),
            "KDIVE_RESPONSE": response,
            "KDIVE_ENV_PROBE": str(probe),
            "KDIVE_LIFECYCLE_EXPECTED_SLOTS": expected_slots,
            "KDIVE_DATABASE_URL": "generic-canary",
            "KDIVE_MIGRATION_DATABASE_URL": "migration-canary",
            "KDIVE_SERVER_DATABASE_URL": "server-canary",
            "KDIVE_RECONCILER_DATABASE_URL": "reconciler-canary",
            "KDIVE_WORKER_DATABASE_URL": "worker-canary",
        },
    )


def _response(*, ok: bool, code: str, slots: list[dict[str, object]] | None = None) -> str:
    """Build a validated wire response without duplicating JSON serialization in Bash."""
    from kdive.processes.lifecycle.systemd.systemd_worker_contract import LifecycleResponse

    return LifecycleResponse.model_validate(
        {
            "ok": ok,
            "code": code,
            "message": "stubbed lifecycle result",
            "retry_action": "retry_same_operation" if not ok else "none",
            "slots": slots or [],
        }
    ).model_dump_json()


def _slot(slot: int, phase: str = "started") -> dict[str, object]:
    return {
        "slot": slot,
        "unit": f"kdive-live-worker@{slot}.service",
        "phase": phase,
    }


def _grafana_supports_arch(arch: str) -> bool:
    """Source lib.sh and return the exit status of `grafana_supports_arch <arch>` as a bool."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{ROOT}/scripts/live-stack/lib.sh" && grafana_supports_arch "$1"',
            "_",
            arch,
        ],
        check=False,
    )
    return result.returncode == 0


def _require_free_http_port(port: int) -> subprocess.CompletedProcess[str]:
    """Source lib.sh and run `require_free_http_port` with KDIVE_HTTP_PORT=<port>."""
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{ROOT}/scripts/live-stack/lib.sh" '
            f'&& KDIVE_HTTP_PORT="$1" require_free_http_port',
            "_",
            str(port),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@contextmanager
def _listening_port() -> Generator[int]:
    """Hold a real LISTEN socket open on a loopback port for the duration of the block."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield sock.getsockname()[1]


def _free_port() -> int:
    """Return a port number that is free at call time (bound then released)."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _lib(snippet: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Source lib.sh and run `snippet`, with `env` overlaid on the current environment."""
    return subprocess.run(
        ["bash", "-c", f'source "{ROOT}/scripts/live-stack/lib.sh"\n{snippet}'],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
    )


def test_configured_worker_count_defaults_to_one_and_rejects_nonsense() -> None:
    """The knob must fail loud on a value that would silently start the wrong number of workers."""
    assert _lib("configured_worker_count").stdout == "1"
    assert _lib("configured_worker_count", KDIVE_WORKER_COUNT="3").stdout == "3"
    # Empty reads as unset, as every other knob in lib.sh does (`${VAR:-default}`).
    assert _lib("configured_worker_count", KDIVE_WORKER_COUNT="").stdout == "1"
    for bad in ("0", "-1", "abc", "2.5"):
        result = _lib("configured_worker_count", KDIVE_WORKER_COUNT=bad)
        assert result.returncode != 0, f"{bad!r} must be rejected"
        assert "positive integer" in result.stderr


def test_configured_worker_count_is_ceilinged() -> None:
    """Each worker is a root process with its own pool; the loop asks for no confirmation.

    The aux port the runbook prints a few lines from the knob is 9470, so a transposition typo
    into the count would fork thousands of root processes on the operator's host.
    """
    assert _lib("configured_worker_count", KDIVE_WORKER_COUNT="8").stdout == "8"
    # 99999999999999999999 wraps POSITIVE in bash's int64 arithmetic (mod 2^64 ->
    # 7766279631452241919), so it is still greater than the ceiling and is rejected on the
    # ordinary path. It is kept because that is a real operator typo, but it does not reach the
    # wrap defect — see test_configured_worker_count_rejects_an_int64_wrapping_value.
    for over in ("9", "9470", "99999999999999999999"):
        result = _lib("configured_worker_count", KDIVE_WORKER_COUNT=over)
        assert result.returncode != 0, f"{over!r} must be refused"
        assert "ceiling" in result.stderr, result.stderr


def test_configured_worker_count_rejects_an_int64_wrapping_value() -> None:
    """A value that wraps NEGATIVE slipped the ceiling entirely — a distinct failure mode.

    The regex bounds sign and format but not magnitude, and bash arithmetic is 64-bit signed, so
    `((count <= MAX_WORKER_COUNT))` was true for anything that wrapped below zero. 2^63 is the
    minimal such value, landing exactly on INT64_MIN. It did not merely bypass the bound: the
    unwrapped string reached the launch loop, whose `index <= count` ran ZERO times, while
    `DAEMON_COUNT` went negative and disabled the settle gate — so a stack with no workers at all
    reported a *surplus*. The ceiling must therefore bound the value's MAGNITUDE before any
    arithmetic touches it: a numeric bound at either end is evaluated after the wrap and cannot
    see it, which is why 2^64+1 below slips a two-sided numeric check as readily as a one-sided one.
    """
    # Every one of these defeats a purely numeric check, because each has already wrapped by the
    # time `((...))` sees it: 2^63 -> INT64_MIN, 2^64 -> 0, and 2^64+1 / 2^64+8 land squarely
    # INSIDE the accepted 1..8 range. The last two are why a two-sided numeric bound is not
    # enough on its own and the digit-count test carries the magnitude. A value that wraps back
    # to a large positive (e.g. 1e20) does NOT belong here — the upper bound catches it, so it
    # would pass this test with or without the fix.
    for wrapping in (
        "9223372036854775808",
        "18446744073709551616",
        "18446744073709551617",
        "18446744073709551624",
    ):
        result = _lib("configured_worker_count", KDIVE_WORKER_COUNT=wrapping)
        assert result.returncode != 0, f"{wrapping!r} wraps past int64 and must be refused"
        assert "ceiling" in result.stderr, result.stderr
        assert not result.stdout, f"a refused count must print nothing, got {result.stdout!r}"


def test_ordinary_daemon_settle_shortage_returns_without_removed_worker_state() -> None:
    result = _lib(
        "set -u\n"
        "sleep() { :; }\n"
        "daemon_pids() { :; }\n"
        "DAEMON_COUNT=1\n"
        "wait_for_daemons_to_settle\n",
    )
    assert result.returncode == 1
    assert "unbound variable" not in result.stderr


def _stop_daemons_signals(pid_expr: str) -> str:
    """Run `stop_daemons` against `pid_expr` as the daemon scan; return the signals it sent.

    `kill` and `sudo` are stubbed to a log so the ownership branch is observable without the test
    signalling anything real, and `sleep` is stubbed so the ten-second settle poll — whose scan
    keeps returning the same pid here — costs nothing. `daemon_pids` is stubbed because the real
    one reads the live process table; `ps` is NOT stubbed, so the ownership test under scrutiny
    runs against real uids.
    """
    return _lib(
        "log=$(mktemp)\n"
        "sleep() { :; }\n"
        f"daemon_pids() {{ echo {pid_expr}; }}\n"
        'kill() { echo "KILL $*" >> "$log"; }\n'
        'sudo() { echo "SUDO $*" >> "$log"; }\n'
        "stop_daemons >/dev/null 2>&1\n"
        'cat "$log"\nrm -f "$log"\n'
    ).stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="a root caller needs sudo for nothing")
def test_unknown_ownership_answers_sudo_even_under_pipefail() -> None:
    """When ps reports no owner, the safe answer is sudo — and pipefail must not invert it.

    `sudo kill` still works on a self-owned process; a bare `kill` does not work on a foreign one,
    so an undetermined owner is safe in exactly one direction. The pipefail arm is the reason the
    helper reads ps into a variable instead of piping it: `ps -o uid= -p <gone>` exits 1, so a
    pipeline's status would come from ps rather than from the comparison and would silently answer
    "no sudo" for precisely the case that cannot determine an owner. The callers run under
    `set -euo pipefail`, so the bare-source case alone would not have caught it.
    """
    for prelude in ("", "set -euo pipefail\n"):
        result = _lib(f"{prelude}if pids_need_sudo 999999; then echo SUDO; else echo BARE; fi")
        assert result.stdout.strip() == "SUDO", (
            f"an undeterminable owner must answer sudo (prelude={prelude!r}): {result.stdout!r}"
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="the self-owned arm needs a non-root caller")
def test_stop_daemons_drops_sudo_for_a_daemon_the_caller_owns() -> None:
    """stop_daemons must not reach for sudo to signal the operator's own daemon (#1739).

    Under `KDIVE_WORKER_AS_ROOT=0` every daemon is the operator's, and on a host with no sudo
    installed a `sudo kill` simply fails — silently, since the call swallows its status with
    `|| true`. This shell's own pid stands in for such a daemon.
    """
    assert _stop_daemons_signals("$$").startswith("KILL "), "a self-owned daemon needs no sudo"


@pytest.mark.skipif(os.geteuid() == 0, reason="a root caller needs sudo for nothing")
def test_stop_daemons_keeps_sudo_for_a_daemon_the_caller_cannot_signal() -> None:
    """The complement: an unsignalable daemon must still get sudo, or the kill silently no-ops.

    pid 1 is root-owned on any host this runs on. The gap this closes is wider than root, though —
    a daemon owned by another *non-root* account is equally unsignalable, which is why the helper
    compares uids against the caller's rather than testing for root. That third case needs a second
    account to construct, so it is not reachable from an unprivileged test; the two directions
    pinned here plus the uid comparison itself are what carry it.
    """
    assert _stop_daemons_signals("1").startswith("SUDO kill "), (
        "a daemon the caller cannot signal must be killed through sudo"
    )


def test_the_worker_count_ceiling_is_documented_where_operators_read_it() -> None:
    """The documented bound must track MAX_WORKER_COUNT rather than drift from it (#1739).

    lib.sh holds the only copy of the ceiling, and exceeding it is a hard bring-up failure, so an
    operator who reads either doc surface and picks a larger value gets no forewarning. Neither
    surface was bound to the constant: `check_env_documented` is a name-set guard that never reads
    description text, and MAX_WORKER_COUNT is not a KDIVE_* token, so it sits outside that guard
    entirely. Read the live value out of lib.sh — sourcing it, so a moved or reformatted assignment
    still resolves — and require both surfaces to state it. A future bump then reddens here instead
    of silently re-opening the gap.
    """
    ceiling = _lib('printf %s "$MAX_WORKER_COUNT"').stdout
    assert ceiling.isdigit(), f"MAX_WORKER_COUNT must be a plain integer, got {ceiling!r}"

    help_text = next(var.help for var in EXTERNAL_ENV_VARS if var.name == "KDIVE_WORKER_COUNT")
    assert f"above {ceiling} are refused" in help_text, (
        f"the KDIVE_WORKER_COUNT help must state the {ceiling} ceiling — it is the source the "
        f"generated config reference renders from: {help_text!r}"
    )

    runbook = (ROOT / "docs/operating/runbooks/live-testing.md").read_text()
    assert f"Values above {ceiling} are refused" in runbook, (
        f"the live-testing runbook drives KDIVE_WORKER_COUNT and must state the {ceiling} ceiling"
    )


def test_lifecycle_slots_remain_bounded_by_the_fixed_worker_range() -> None:
    """Worker 1 keeps the process default; extras must not land on ANOTHER process's port.

    uvicorn's bind is exclusive, so an extra worker that reused 9465 — or stepped up onto the
    reconciler's 9466 — would die at startup instead of claiming jobs, and the multi-worker stack
    would silently degrade back to the single-worker serialization this knob exists to escape.
    """
    lifecycle = (ROOT / "scripts/live-stack/worker-lifecycle.sh").read_text()
    assert '[[ "${2:-}" =~ ^[1-8]$ ]]' in lifecycle


def test_lifecycle_client_assigns_distinct_extra_worker_health_ports() -> None:
    """An explicit bind wins for EVERY process, so it cannot coexist with more than one worker.

    Both accommodations are wrong: honouring the operator's port walks the extras onto the
    registered server and reconciler defaults, and ignoring it silently discards the setting for
    every worker but the first. Neither yields a stack that comes up, so bring-up must refuse and
    name the knob to drop rather than start workers that die on an exclusive bind.
    """
    lifecycle = (ROOT / "scripts/live-stack/worker-lifecycle.sh").read_text()
    assert "9465 if slot == 1 else 9468 + slot" in lifecycle


def test_worker_logs_are_owned_by_the_lifecycle_witness() -> None:
    """Worker 1 keeps the name recorded runbooks and proof records already cite."""
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert "worker_log_path" not in text


def _build_stamps(tmp_path: Path, logs: dict[str, str]) -> list[str]:
    """Run report_build_stamps against a seeded log dir; return its output lines.

    `py` is stubbed to a path nothing can be running from, so the live-worker count is a property
    of the fixture rather than of the developer's machine. Without it this test reads the host
    process table and goes red whenever this worktree's own live stack is up — which is exactly
    the state the new runbook arm tells the operator to create.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    for name, body in logs.items():
        (log_dir / name).write_text(body)
    result = _lib(f'py="{tmp_path}/no-such-python"\nlog_dir="{log_dir}"\nreport_build_stamps\n')
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_build_stamps_report_only_ordinary_host_daemons(tmp_path: Path) -> None:
    """Every worker gets a row, and the header states how many are actually alive.

    The deferral record for the ADR-0482 preflight's single-worker probe set nominates this
    block as the standing mitigation, so it has to be readable in both directions: a worker
    running different code must not be omitted, and a stale log left by a stack that has since
    been downgraded must not read as a live, graded process.
    """
    stamp = '{"msg": "starting kdive 0.4.1-dev+gcafe1234 (worker)"}\n'
    rows = _build_stamps(
        tmp_path,
        {"worker-root.log": stamp, "worker-root-2.log": stamp, "server.log": stamp},
    )
    header = rows[0]
    assert "build stamps" in header and "worker process(es)" not in header, header
    labels = [line.split()[0] for line in rows[1:] if line.startswith("  ")]
    assert labels == ["server", "reconciler"], labels
    # Nothing runs from the stubbed interpreter, so the count exposes both rows as stale logs
    # rather than as graded processes — the direction a file-only enumeration cannot report.
    assert "worker process(es)" not in header, header


def test_bring_up_waits_for_lifecycle_status_after_start(tmp_path: Path) -> None:
    """The settle gate counts a host-wide total, so it cannot see a missing worker on its own.

    `daemon_pids` is deliberately checkout-agnostic, and `stop_daemons` warns rather than fails
    after ten seconds — so a survivor from another worktree makes 2 + N add up while one of THIS
    checkout's workers is dead. Bring-up would exit 0 on a stack that silently serializes, which
    is the whole failure the knob exists to escape. The count must be asserted on workers.
    """
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert text.index('worker-lifecycle.sh" start') < text.index('worker-lifecycle.sh" status')


def test_bring_up_uses_the_witness_instead_of_direct_worker_pids(tmp_path: Path) -> None:
    """A survivor of stop_daemons is from THIS checkout, so `>=` would let it mask a dead worker.

    stop_daemons warns and returns 0 after ten seconds, and a worker ignores SIGTERM until its
    job ends — which the contention arm arranges by parking workers inside a multi-GiB fetch. So
    a leftover worker is the expected state here, not an edge case, and it may be running older
    code. The two directions need different remedies, so the surplus must fail on its own.
    """
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert "worker_pids" not in text


def test_witness_stop_precedes_the_ordinary_host_daemon_stop(tmp_path: Path) -> None:
    """The remedy must use forced teardown, which can end the worker this message is about.

    Plain teardown remains graceful-only. The explicit force option escalates after that grace
    period without asking the operator to reproduce pid discovery and privilege handling.
    """
    text = (ROOT / "scripts/live-stack/stack-down.sh").read_text()
    assert text.index('worker-lifecycle.sh" stop') < text.index("stop_daemons")
    # Not a bare `"wait" in stderr` — the pre-fix message already said "the ten-second wait only
    # warns", so that substring passes against the very message this test exists to reject.
    # Killing abandons a running job, so the message must not stop at the command: it has to say
    # what picks up the pieces, or the operator is left guessing whether they have to wipe. The
    # mechanism is the queue, NOT the reconciler — `dequeue` reclaims a `running` row whose lease
    # has lapsed and charges an attempt. The message deliberately claims nothing beyond that: two
    # earlier drafts asserted a downstream cleanup path the source contradicted, so the scope of
    # what it promises is itself the thing under test.
    # The clause spans the message's line wrap, so compare against a whitespace-normalized copy:
    # the assertion is that the sentence is present and in order, not that the wrap falls anywhere
    # in particular. Pinning the wrap point makes an unrelated rewording redden this for no reason.
    # The pid list is every worker sharing this interpreter, not only the survivor — telling the
    # operator otherwise sends them to kill -9 a set the prose has mislabelled.


def test_lifecycle_stop_failure_blocks_backend_teardown(tmp_path: Path) -> None:
    """One scan, or the count and the pid list the operator is told to kill can disagree.

    Counting with one `ps` and printing with a second lets a worker exit in between, so the
    message reports a count its own pid list contradicts — and that list is what the remedy
    tells the operator to act on. The stub here returns two pids to the first scan and one to
    every scan after it, which is exactly that interleaving.
    """
    text = (ROOT / "scripts/live-stack/stack-down.sh").read_text()
    assert "unresolved evidence; backends remain up" in text


def test_stop_daemons_warns_with_the_set_it_actually_polled(tmp_path: Path) -> None:
    """The WARN must report the scan that decided to warn, not a fresh one taken after it.

    A second `ps` there is the same skew just fixed in require_workers_alive, one function below:
    a daemon exiting between the last poll and the WARN leaves the operator a list that never
    matched the check that produced it. The stub returns a different set once the poll loop is
    over, so any trailing scan shows up in the message.

    This function really signals pids, and 111/222 belong to someone on any busy host — on the
    machine this was written, 222 was a root kernel thread. Both kill paths must therefore be
    stubbed, and a shell function covers only one of them: in `sudo kill "$pid"` the `kill` is an
    argument to sudo, not a command the shell resolves, so a `kill()` function never sees it and
    the root-owner branch would run for real. Which branch is taken otherwise depends on whether
    111/222 exist on the host and who owns them, so `id` is stubbed to pin it to the same-user
    branch that `kill()` covers; `sudo` is stubbed on PATH regardless, so even a future change to
    that condition cannot signal a real process. `sleep()` keeps the ten-second poll free.
    """
    counter = tmp_path / "scans"
    sudo_stub = tmp_path / "sudo"
    sudo_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"{tmp_path}/sudo-argv"\n')
    sudo_stub.chmod(0o755)
    result = _lib(
        "kill() { :; }\n"
        "sleep() { :; }\n"
        "id() { echo root; }\n"
        f'daemon_pids() {{ echo scan >>"{counter}"\n'
        f'  if (( $(wc -l <"{counter}") > 21 )); then echo 999; else printf "111\\n222\\n"; fi\n'
        "}\n"
        "stop_daemons\n",
        PATH=f"{tmp_path}:{os.environ['PATH']}",
    )
    # One scan builds the kill list, then the poll loop runs 20 times. A 22nd means the WARN
    # went back to `ps` instead of reusing what the loop had already read.
    assert counter.read_text().count("scan") == 21, counter.read_text().count("scan")
    assert "still running after stop: 111 222" in result.stderr, result.stderr
    assert "999" not in result.stderr, (
        f"the WARN re-scanned after the poll loop instead of reusing it: {result.stderr}"
    )


def test_stop_daemons_names_pids_that_never_received_sigterm() -> None:
    """A failed signal and an ignored signal need different operator remedies (#1733)."""
    result = _lib(
        "sleep() { :; }\n"
        "daemon_pids() { echo 111; echo 222; }\n"
        'kill() { [[ "$1" != "111" ]]; }\n'
        "pids_need_sudo() { return 1; }\n"
        "stop_daemons\n"
    )
    assert "SIGTERM was not delivered to: 111" in result.stderr, result.stderr
    assert "daemons still running after stop: 111 222" in result.stderr, result.stderr


def test_force_stop_daemons_sends_sigkill_only_to_graceful_survivors(tmp_path: Path) -> None:
    """The force helper is the teardown-only escalation primitive (#1733)."""
    scans = tmp_path / "scans"
    signals = tmp_path / "signals"
    result = _lib(
        "sleep() { :; }\n"
        f'daemon_pids() {{ echo scan >>"{scans}"; '
        f'(( $(wc -l <"{scans}") <= 3 )) && printf "111\\n222\\n"; }}\n'
        "pids_need_sudo() { return 1; }\n"
        f'kill() {{ echo "$*" >>"{signals}"; }}\n'
        "force_stop_daemons\n"
    )
    assert result.returncode == 0, result.stderr
    assert signals.read_text().splitlines() == ["-9 111", "-9 222"]


def test_force_stop_daemons_fails_when_sigkill_cannot_be_delivered() -> None:
    """Forced teardown must not claim success and stop backends after signal failure."""
    result = _lib(
        "sleep() { :; }\n"
        "daemon_pids() { echo 111; }\n"
        "pids_need_sudo() { return 1; }\n"
        "kill() { return 1; }\n"
        "force_stop_daemons\n"
    )
    assert result.returncode != 0
    assert "SIGKILL was not delivered to: 111" in result.stderr, result.stderr


def test_force_stop_daemons_revalidates_a_pid_before_sigkill(tmp_path: Path) -> None:
    """A daemon that exits after discovery must not expose a reused pid to SIGKILL."""
    scans = tmp_path / "scans"
    signals = tmp_path / "signals"
    result = _lib(
        "sleep() { :; }\n"
        f'daemon_pids() {{ echo scan >>"{scans}"; '
        f'[[ $(wc -l <"{scans}") == 1 ]] && echo 111; }}\n'
        "pids_need_sudo() { return 1; }\n"
        f'kill() {{ echo "$*" >>"{signals}"; }}\n'
        "force_stop_daemons\n"
    )
    assert result.returncode == 0, result.stderr
    assert not signals.exists(), "a pid absent from the revalidated daemon set must not be killed"


def test_down_force_is_teardown_only_and_runs_after_the_graceful_stop() -> None:
    """Bring-up keeps graceful signalling; only stack-down.sh wires in escalation (#1733)."""
    down = (ROOT / "scripts/live-stack/stack-down.sh").read_text()
    up = (ROOT / "scripts/live-stack/stack-services.sh").read_text()
    assert down.index("stop_daemons\n") < down.index("force_stop_daemons\n")
    assert '[[ "$force" == "1" ]]' in down
    assert "force_stop_daemons" not in up


def test_down_force_stops_backends_only_after_forced_daemon_stop(tmp_path: Path) -> None:
    """The CLI executes the lifecycle stop and the supported force path before compose teardown."""
    script_dir = tmp_path / "scripts" / "live-stack"
    script_dir.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/live-stack/stack-down.sh", script_dir / "stack-down.sh")
    events = tmp_path / "events"
    lifecycle = script_dir / "worker-lifecycle.sh"
    lifecycle.write_text(
        f'#!/bin/sh\n[ "$1" = stop ] && echo lifecycle >>"{events}"\n',
        encoding="utf-8",
    )
    lifecycle.chmod(0o755)
    (script_dir / "lib.sh").write_text(
        f'repo_root="{tmp_path}"\n'
        f'stop_daemons() {{ echo graceful >>"{events}"; }}\n'
        f'force_stop_daemons() {{ echo force >>"{events}"; }}\n'
        f'docker() {{ echo docker >>"{events}"; }}\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(script_dir / "stack-down.sh"), "--force"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert events.read_text().splitlines() == ["lifecycle", "graceful", "force", "docker"]


def test_down_force_keeps_backends_up_when_forced_daemon_stop_fails(tmp_path: Path) -> None:
    """A failed SIGKILL path must not dismantle dependencies under a live worker."""
    script_dir = tmp_path / "scripts" / "live-stack"
    script_dir.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/live-stack/stack-down.sh", script_dir / "stack-down.sh")
    events = tmp_path / "events"
    lifecycle = script_dir / "worker-lifecycle.sh"
    lifecycle.write_text(
        f'#!/bin/sh\n[ "$1" = stop ] && echo lifecycle >>"{events}"\n',
        encoding="utf-8",
    )
    lifecycle.chmod(0o755)
    (script_dir / "lib.sh").write_text(
        f'repo_root="{tmp_path}"\n'
        f'stop_daemons() {{ echo graceful >>"{events}"; }}\n'
        f'force_stop_daemons() {{ echo force >>"{events}"; return 1; }}\n'
        f'docker() {{ echo docker >>"{events}"; }}\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(script_dir / "stack-down.sh"), "--force"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert events.read_text().splitlines() == ["lifecycle", "graceful", "force"]


def test_lifecycle_stop_failure_names_the_recovery_path(tmp_path: Path) -> None:
    """Waiting is not offered when the preceding SIGTERM never reached a worker."""
    text = (ROOT / "scripts/live-stack/stack-down.sh").read_text()
    assert "restore the failed dependency and retry" in text


def test_lifecycle_force_path_states_its_evidence_limit(tmp_path: Path) -> None:
    """Host-wide stop failures must not change advice for checkout-scoped worker pids."""
    text = (ROOT / "scripts/live-stack/stack-down.sh").read_text()
    assert "cannot publish worker termination evidence" in text


def test_build_stamps_report_only_ordinary_host_processes_without_logs(tmp_path: Path) -> None:
    """Worker journal ownership belongs to the lifecycle witness, not the launcher."""
    rows = _build_stamps(tmp_path, {})
    labels = [line.split()[0] for line in rows[1:] if line.startswith("  ")]
    assert labels == ["server", "reconciler"], labels
    assert all("<no startup log line>" in line for line in rows[1:]), rows


def test_restart_host_processes_uses_one_lifecycle_worker_by_default(tmp_path: Path) -> None:
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert 'local count="${KDIVE_WORKER_COUNT:-1}"' in text


def test_restart_host_processes_passes_the_configured_count_to_lifecycle(tmp_path: Path) -> None:
    """Three workers must really be launched, each with its own log and its own aux port."""
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert 'worker-lifecycle.sh" start "$worker_count"' in text


def test_root_worker_launch_is_removed(tmp_path: Path) -> None:
    """The DEFAULT launch branch is the sudo-root one, and it is what the runbook arm runs.

    The per-worker bind is spliced into a quoted `sudo bash -c` string there, and each extra
    worker must get its OWN bind — a regression that gives them one port degrades a two-worker
    stack back to serialization, which is the failure the whole change exists to remove.

    The ordering assertion below is a cheap guard, not a live constraint: env.sh does not mention
    KDIVE_HEALTH_BIND_ADDR at all today, so nothing re-defaults over the export and either order
    would work. It is asserted so that the export stays the last writer if env.sh ever grows a
    `:-` default for that variable, as it already has for the other forwarded vars.
    """
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert "sudo bash -c" not in text
    assert "start_worker" not in text


def test_live_stack_env_exports_required_defaults() -> None:
    env = (ROOT / "scripts/live-stack/env.sh").read_text()
    required = [
        "KDIVE_MIGRATION_DATABASE_URL",
        "KDIVE_SERVER_DATABASE_URL",
        "KDIVE_WORKER_DATABASE_URL",
        "KDIVE_RECONCILER_DATABASE_URL",
        "KDIVE_OIDC_ISSUER",
        "KDIVE_OIDC_JWKS_URI",
        "KDIVE_OIDC_AUDIENCE",
        "KDIVE_S3_ENDPOINT_URL",
        "KDIVE_S3_BUCKET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "KDIVE_BUILD_WORKSPACE",
        "KDIVE_BUILD_COMPONENT_ROOTS",
        "KDIVE_INSTALL_STAGING",
        "KDIVE_STACK_BASE_URL",
        # Configurable compose backend host ports (single source of truth for publish + client URL).
        "KDIVE_POSTGRES_PORT",
        "KDIVE_SEAWEEDFS_PORT",
        "KDIVE_OIDC_PORT",
        "KDIVE_PROMETHEUS_PORT",
        "KDIVE_GRAFANA_PORT",
    ]
    for name in required:
        assert f"export {name}=" in env


def test_live_stack_lane_default_matches_the_process_default() -> None:
    """Keep the host-side lane fallback equal to the process default (#2058).

    Provision jobs persist on ``default``; restore/reprovision/snapshot use ``state-fenced``.
    Matching the full process default keeps every active kind claimable without repeating a false
    provision-routing premise.
    """
    env = (ROOT / "scripts/live-stack/env.sh").read_text()
    assert (
        f'export KDIVE_ACCEPTED_LANES="${{KDIVE_ACCEPTED_LANES:-{_DEFAULT_ACCEPTED_LANES}}}"' in env
    )


def test_live_stack_rootfs_default_reaches_child_processes() -> None:
    result = _lib("bash -c 'printf %s \"${KDIVE_ROOTFS_DIR-unset}\"'")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "/var/lib/kdive/rootfs"


_PUBLISHED_URI = "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"


def _libvirt_env(**overrides: str) -> dict[str, str]:
    """The ambient environment with ``KDIVE_LIBVIRT_URI`` *removed*, plus `overrides`.

    Removed rather than emptied: bash keeps the export attribute of a variable it inherited, so
    an inherited empty value would make an unexported assignment look exported and every export
    assertion below would pass against the very defect they exist to catch. It also keeps these
    tests host-independent on a provisioned box, where the value is ambient.
    """
    env = {k: v for k, v in os.environ.items() if k != "KDIVE_LIBVIRT_URI"}
    env.update(overrides)
    return env


def _published_contract(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Stage a `live-worker-libvirt.env` fixture and the environment that reaches it.

    ``require_exact_libvirt_env`` demands root:root 0644, which no test can produce for a file it
    owns, so a `stat` shim on PATH answers that one probe. The fixture's *content* still goes
    through the real parser and its two-URI allowlist, which is what actually bounds a redirected
    ``LIBVIRT_ENV`` -- the ownership probe is a PATH lookup and so is answerable by any caller who
    can set ``LIBVIRT_ENV`` in the same invocation. It guards a tampered /etc entry, not the
    caller who chose the path.
    """
    contract = tmp_path / "live-worker-libvirt.env"
    contract.write_text(f"KDIVE_LIBVIRT_URI={_PUBLISHED_URI}\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stat_stub = bin_dir / "stat"
    stat_stub.write_text("#!/bin/sh\nprintf '0:0:644\\n'\n", encoding="utf-8")
    stat_stub.chmod(0o755)
    return contract, _libvirt_env(LIBVIRT_ENV=str(contract), PATH=f"{bin_dir}:{os.environ['PATH']}")


def _sourced(script: Path, snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Source `script` and run `snippet` under exactly `env` (no ambient merge)."""
    return subprocess.run(
        ["bash", "-c", f'source "{script}"\n{snippet}'],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_live_stack_libvirt_uri_reaches_child_processes(tmp_path: Path) -> None:
    """#2480: the assignment carried no `export`, so `restart_host_processes`' server and
    reconciler forks never received it and fell back to the in-process qemu:///system default
    while the lifecycle worker used the published session URI."""
    result = _sourced(
        ROOT / "scripts/live-stack/lib.sh",
        "bash -c 'printf %s \"${KDIVE_LIBVIRT_URI-unset}\"'",
        _libvirt_env(LIBVIRT_ENV=str(tmp_path / "absent" / "live-worker-libvirt.env")),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "qemu:///system"


@pytest.mark.parametrize(
    ("preset", "published", "expected", "reports"),
    [
        ("", False, "qemu:///system", False),
        ("", True, _PUBLISHED_URI, False),
        # An explicit caller value still wins over both, published contract or not -- but since
        # #2509 one that contradicts a *valid* contract says so on stderr on its way through.
        ("qemu:///system", True, "qemu:///system", True),
        ("qemu+ssh://elsewhere/system", False, "qemu+ssh://elsewhere/system", False),
    ],
)
def test_live_stack_env_resolves_one_libvirt_endpoint(
    tmp_path: Path, preset: str, published: bool, expected: str, reports: bool
) -> None:
    """env.sh set no libvirt endpoint at all before #2480, so the bare runbook invocation of
    stack-services.sh left the daemons on a different endpoint from the worker's. Reading the
    value back out of a child process also proves it is exported, not merely assigned."""
    contract, staged = _published_contract(tmp_path)
    if not published:
        contract.unlink()
    if preset:
        staged["KDIVE_LIBVIRT_URI"] = preset
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        "bash -c 'printf %s \"${KDIVE_LIBVIRT_URI-unset}\"'",
        staged,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    # Which value the resolver settles on is unchanged by the report; only stderr moves.
    assert (result.stderr != "") is reports, result.stderr


def test_a_preset_contradicting_the_contract_is_reported(tmp_path: Path) -> None:
    """#2509: the preset branch short-circuited without ever reading the published contract, so a
    preset that disagreed with it put the operator's shell and the worker processes on different
    daemons with no message -- the #2480 split, reached through the one path still allowed to be
    silent. ADR-0661: report it, do not refuse it.

    The report has to name both values and the way back. A bare "these disagree" leaves the
    operator with the fact that made the contract unreadable in the first place.
    """
    _, staged = _published_contract(tmp_path)
    staged["KDIVE_LIBVIRT_URI"] = "qemu:///system"
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        "bash -c 'printf %s \"${KDIVE_LIBVIRT_URI-unset}\"'",
        staged,
    )
    # Honoured, not refused: the exported value a child sees is still the operator's.
    assert result.returncode == 0, result.stderr
    assert result.stdout == "qemu:///system"
    assert "qemu:///system" in result.stderr
    assert _PUBLISHED_URI in result.stderr
    assert "KDIVE_LIBVIRT_URI" in result.stderr


def test_a_preset_matching_the_contract_is_not_reported(tmp_path: Path) -> None:
    """The guard keys on the *value*, not on the preset's presence.

    `.github/workflows/live.yml` and the self-hosted runner runbook both preset the endpoint to
    `$(load_published_libvirt_uri)` -- agreeing presets, on every live CI run. A presence-keyed
    guard would report all of them.
    """
    _, staged = _published_contract(tmp_path)
    staged["KDIVE_LIBVIRT_URI"] = _PUBLISHED_URI
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        "bash -c 'printf %s \"${KDIVE_LIBVIRT_URI-unset}\"'",
        staged,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == _PUBLISHED_URI
    assert result.stderr == ""


@pytest.mark.parametrize(
    "leg", ("absent", "untrusted-metadata", "malformed-line-shape", "allowlist-refused")
)
def test_a_preset_is_honoured_silently_without_a_valid_contract(tmp_path: Path, leg: str) -> None:
    """Every loader refusal, on the path that exists to get past exactly that state.

    An explicit override is how an operator works on a host whose contract is broken or absent,
    so the comparison must not turn the escape hatch into the thing it escapes. Two ways it
    could: `load_published_libvirt_uri` writes its refusal to stderr *before* returning 1, and
    under the callers' `set -euo pipefail` a bare assignment from a failing command substitution
    aborts the sourcing shell. Both halves are asserted here -- empty stderr and exit 0.

    Four inputs across the loader's three refusals, rather than one input each: `absent` and
    `untrusted-metadata` both land on `require_exact_libvirt_env`, which is one refusal reached
    two ways, and covering only those two would leave the line-shape refusal unexercised.
    """
    contract, staged = _published_contract(tmp_path)
    if leg == "absent":
        contract.unlink()
    elif leg == "untrusted-metadata":
        (tmp_path / "bin" / "stat").write_text(
            "#!/bin/sh\nprintf '1000:1000:644\\n'\n", encoding="utf-8"
        )
    elif leg == "malformed-line-shape":
        contract.write_text(
            f"KDIVE_LIBVIRT_URI={_PUBLISHED_URI}\nHOST=elsewhere\n", encoding="utf-8"
        )
    else:
        contract.write_text("KDIVE_LIBVIRT_URI=qemu:///wrong\n", encoding="utf-8")
    staged["KDIVE_LIBVIRT_URI"] = "qemu+ssh://elsewhere/system"
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        "bash -c 'printf %s \"${KDIVE_LIBVIRT_URI-unset}\"'",
        staged,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "qemu+ssh://elsewhere/system"
    assert result.stderr == ""


@pytest.mark.parametrize("metadata", ("1000:1000:644", "0:0:664", "0:1000:644"))
def test_a_contract_not_owned_by_root_is_refused_not_downgraded(
    tmp_path: Path, metadata: str
) -> None:
    """Cover the ownership leg of require_exact_libvirt_env, which this change made load-bearing.

    Since #2480 that predicate decides whether resolve_libvirt_uri aborts the sourcing shell, so
    it now gates stack-services.sh, stack-down.sh and stack-status.sh alike. Nothing exercised it
    before: the one test that reached it stubbed it out. The `stat` shim the other tests use to
    satisfy it is what makes the negative case testable here as well -- it prints the metadata
    under test instead of the test user's real ownership.
    """
    _, staged = _published_contract(tmp_path)
    (tmp_path / "bin" / "stat").write_text(f"#!/bin/sh\nprintf '{metadata}\\n'\n", encoding="utf-8")
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        'printf %s "${KDIVE_LIBVIRT_URI-unset}"',
        staged,
    )
    assert result.returncode != 0
    assert result.stdout != "qemu:///system"
    assert "untrusted metadata" in result.stderr


@pytest.mark.parametrize("target", ("missing", "live-worker-libvirt.env"))
def test_a_symlink_at_the_contract_path_is_refused_not_downgraded(
    tmp_path: Path, target: str
) -> None:
    """A symlink occupying the contract path must abort, never resolve qemu:///system.

    `-e` follows symlinks, so a *dangling* one reads as absent and would take the default branch
    -- a silent downgrade to the very endpoint the split is about, reached through the one
    condition require_exact_libvirt_env exists to refuse. A symlink to a valid contract is
    refused by that check either way; both shapes belong here so the gate cannot be narrowed
    back to `-e` alone without one of them going red.
    """
    contract, staged = _published_contract(tmp_path)
    link = tmp_path / "linked.env"
    link.symlink_to(tmp_path / target)
    staged["LIBVIRT_ENV"] = str(link)
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        'printf %s "${KDIVE_LIBVIRT_URI-unset}"',
        staged,
    )
    assert result.returncode != 0
    assert result.stdout != "qemu:///system"
    assert "untrusted metadata" in result.stderr
    assert "export KDIVE_LIBVIRT_URI" in result.stderr
    assert contract.exists()


def _broken_contract(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Stage a contract whose metadata passes but whose content the allowlist refuses.

    The ownership probe is answered by the same `stat` shim `_published_contract` installs, so
    what fails here is the parser's two-URI allowlist -- the failure an operator actually meets
    when /etc/kdive/live-worker-libvirt.env has drifted, rather than the tampered-metadata case
    the tests above cover.
    """
    contract, staged = _published_contract(tmp_path)
    contract.write_text("KDIVE_LIBVIRT_URI=qemu:///wrong\n", encoding="utf-8")
    return contract, staged


def test_a_libvirt_free_entry_point_survives_a_broken_contract(tmp_path: Path) -> None:
    """ADR-0659: LIBVIRT_OPTIONAL downgrades the source-time abort to a recorded degraded state.

    The endpoint is left UNSET rather than empty, because `virsh -c ''` connects to libvirt's
    probed default and exits 0 -- the silent downgrade #2480 exists to prevent. Unset makes every
    unguarded reader die under `set -u` instead.
    """
    contract, staged = _broken_contract(tmp_path)
    staged["LIBVIRT_OPTIONAL"] = "1"
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        'printf "%s|%s" "${KDIVE_LIBVIRT_URI-unset}" "${LIBVIRT_UNRESOLVED}"',
        staged,
    )
    assert result.returncode == 0, result.stderr
    endpoint, reason = result.stdout.split("|", 1)
    assert endpoint == "unset"
    assert str(contract) in reason


def test_the_degraded_state_survives_a_second_source(tmp_path: Path) -> None:
    """stack-status.sh sources lib.sh and env.sh, each calling resolve_libvirt_uri, so the second
    call must neither clear the recorded reason nor repeat the diagnosis."""
    _, staged = _broken_contract(tmp_path)
    staged["LIBVIRT_OPTIONAL"] = "1"
    result = _sourced(
        ROOT / "scripts/live-stack/lib.sh",
        f'set -euo pipefail\nsource "{ROOT}/scripts/live-stack/env.sh"\n'
        'printf %s "${LIBVIRT_UNRESOLVED}"',
        staged,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout, "the second source cleared the degraded reason"
    assert result.stderr.count("failed validation") == 1, result.stderr


def test_a_broken_contract_still_aborts_without_the_declaration(tmp_path: Path) -> None:
    """The opt-out is off by default: an entry point that has not declared itself libvirt-free
    keeps the #2480 fail-closed abort, endpoint and all."""
    _, staged = _broken_contract(tmp_path)
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        'printf %s "${KDIVE_LIBVIRT_URI-unset}"',
        staged,
    )
    assert result.returncode != 0
    assert result.stdout != "qemu:///system"


def test_an_inherited_degraded_record_does_not_suppress_resolution(tmp_path: Path) -> None:
    """LIBVIRT_UNRESOLVED is the resolver's output, never its input.

    `resolve_libvirt_uri`'s first statement short-circuits on it, so a value arriving from the
    environment would leave KDIVE_LIBVIRT_URI unset on a host whose published contract is fine --
    with no opt-out declared by anyone, and with stack-status.sh reporting that healthy host as
    unresolved with a caller-chosen reason string.
    """
    _, staged = _published_contract(tmp_path)
    staged["LIBVIRT_UNRESOLVED"] = "inherited reason"
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        'printf "%s|%s" "${KDIVE_LIBVIRT_URI-unset}" "${LIBVIRT_UNRESOLVED}"',
        staged,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_PUBLISHED_URI}|"


def test_repairing_the_endpoint_clears_the_degraded_record(tmp_path: Path) -> None:
    """The record is the current state, not the first one.

    ADR-0659 sends #2509's next guard into resolve_libvirt_uri's preset-endpoint branch, so a
    caller supplying an endpoint after a degrade is the shape this function is about to grow. A
    write-once record would leave require_libvirt_uri refusing an operation the shell can by then
    perform, and the re-entry guard would skip the #2480 export that supplied value needs.
    """
    _, staged = _broken_contract(tmp_path)
    staged["LIBVIRT_OPTIONAL"] = "1"
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        f'KDIVE_LIBVIRT_URI="{_PUBLISHED_URI}"\n'
        "resolve_libvirt_uri\n"
        'require_libvirt_uri "reap kdive domains"\n'
        'bash -c \'printf "%s|%s" "${KDIVE_LIBVIRT_URI-unset}" "${LIBVIRT_UNRESOLVED-unset}"\'',
        staged,
    )
    assert result.returncode == 0, result.stderr
    # The endpoint reached a child because resolve_libvirt_uri exported it; the record did not,
    # because it is this file's own state and never leaves the shell that computed it.
    assert result.stdout == f"{_PUBLISHED_URI}|unset"


def test_require_libvirt_uri_refuses_while_the_endpoint_is_unresolved(tmp_path: Path) -> None:
    """A libvirt-free entry point still fails closed for the operations that need libvirt --
    at the point of use rather than at source time, and naming which operation was refused."""
    _, staged = _broken_contract(tmp_path)
    staged["LIBVIRT_OPTIONAL"] = "1"
    refused = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        'require_libvirt_uri "reap kdive domains"',
        staged,
    )
    assert refused.returncode != 0
    assert "reap kdive domains" in refused.stderr
    _, resolved = _published_contract(tmp_path)
    resolved["LIBVIRT_OPTIONAL"] = "1"
    allowed = _sourced(
        ROOT / "scripts/live-stack/env.sh", 'require_libvirt_uri "reap kdive domains"', resolved
    )
    assert allowed.returncode == 0, allowed.stderr


def test_the_declaration_reaches_a_child_that_sources_lib_sh(tmp_path: Path) -> None:
    """stack-down.sh spawns `worker-lifecycle.sh stop` before it stops anything, and that child
    sources lib.sh and env.sh itself. A shell-local declaration would never reach it, so the
    child would abort and teardown would exit having stopped nothing."""
    _, staged = _broken_contract(tmp_path)
    staged["LIBVIRT_OPTIONAL"] = "1"
    result = _sourced(
        ROOT / "scripts/live-stack/env.sh",
        f'bash -euo pipefail -c \'source "{ROOT}/scripts/live-stack/lib.sh"; printf %s'
        ' "${KDIVE_LIBVIRT_URI-unset}"\'',
        staged,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "unset"


def _isolated_stack_down(tmp_path: Path, events: Path, lib_extra: str = "") -> Path:
    """Copy stack-down.sh beside stubs that record each teardown step instead of performing it.

    The gate under test stays real: the stub lib.sh sources the shipped libvirt-uri.sh, so the
    declaration, `resolve_libvirt_uri` and `require_libvirt_uri` are the ones being exercised.
    What the stubs replace is the destruction, and that is the point -- the regression these
    tests exist to catch (a --wipe gate removed or moved below the teardown) is precisely the
    one that would otherwise have the suite run `docker compose --profile obs down -v` against
    whatever host is running it.

    `lib_extra` is appended to the stub lib.sh, so a caller that needs the reap to behave a
    particular way -- domains present, an `undefine` that is refused, an unreadable overlay
    directory -- redefines exactly those stubs and inherits the rest.
    """
    script_dir = tmp_path / "scripts" / "live-stack"
    script_dir.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/live-stack/stack-down.sh", script_dir / "stack-down.sh")
    # The child sources lib.sh under its own `set -euo pipefail`, exactly as the real
    # worker-lifecycle.sh does, because that is the only thing the `export` on the declaration
    # buys: a stub that merely echoes cannot abort, so it records the same event list whether or
    # not the keyword is there, and the arm below would assert nothing about it.
    lifecycle = script_dir / "worker-lifecycle.sh"
    lifecycle.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"\n'
        'source "${here}/lib.sh"\n'
        f'echo "lifecycle $1" >>"{events}"\n',
        encoding="utf-8",
    )
    lifecycle.chmod(0o755)
    (script_dir / "lib.sh").write_text(
        f'source "{ROOT}/scripts/live-stack/libvirt-uri.sh"\n'
        "resolve_libvirt_uri\n"
        f'repo_root="{tmp_path}"\n'
        f'KDIVE_ROOTFS_DIR="{tmp_path}/rootfs"\n'
        f'stop_daemons() {{ echo graceful >>"{events}"; }}\n'
        f'force_stop_daemons() {{ echo force >>"{events}"; }}\n'
        f'docker() {{ echo docker >>"{events}"; }}\n'
        f'sudo() {{ echo "sudo $1" >>"{events}"; }}\n' + lib_extra,
        encoding="utf-8",
    )
    return script_dir / "stack-down.sh"


def test_stack_down_refuses_wipe_before_reaching_any_teardown(tmp_path: Path) -> None:
    """--wipe is the one stack-down.sh operation that needs libvirt, so it is refused up front.

    Refusing after the teardown had begun would leave the compose volumes dropped and the domains
    they outlive orphaned -- the pairing stack-down.sh's own header exists to keep. The empty
    event log is the assertion that carries that: not a banner absent from stdout, but no
    teardown step having run at all.
    """
    _, staged = _broken_contract(tmp_path)
    events = tmp_path / "events"
    result = subprocess.run(
        ["bash", str(_isolated_stack_down(tmp_path, events)), "--wipe", "--yes"],
        capture_output=True,
        text=True,
        check=False,
        env=staged,
    )
    assert result.returncode != 0
    assert not events.exists(), events.read_text(encoding="utf-8")
    assert "=== stopping host processes ===" not in result.stdout
    assert "cannot reap kdive domains for --wipe" in result.stderr
    # The shared message's two routes both need the URI the broken contract just made
    # unreadable; the operator whose goal is "get this stack down" needs the third one named.
    assert "re-run without --wipe" in result.stderr


def test_stack_down_completes_plain_teardown_on_a_broken_contract(tmp_path: Path) -> None:
    """The other half of the same criterion: the recovery tool has to finish, not merely start.

    Plain teardown reads no libvirt, so the degraded endpoint must carry it through the lifecycle
    stop, the daemon stop and the compose down. The spawned `worker-lifecycle.sh stop` proves the
    declaration crossed the process boundary: a shell-local one would abort that child, and the
    event log would stop at its first line.
    """
    _, staged = _broken_contract(tmp_path)
    events = tmp_path / "events"
    result = subprocess.run(
        ["bash", str(_isolated_stack_down(tmp_path, events))],
        capture_output=True,
        text=True,
        check=False,
        env=staged,
    )
    assert result.returncode == 0, result.stderr
    steps = events.read_text(encoding="utf-8").splitlines()
    assert steps == ["lifecycle stop", "graceful", "docker"], steps
    assert result.stdout.rstrip().endswith("done")


# A directory of files named for the defined domains stands in for the libvirt host, so the reap's
# end-state re-read observes a removal instead of being told about one: `list` reports whatever is
# still there and `undefine` unlinks the name. `sudo` runs its command rather than recording it, so
# an arm's own `virsh` or `rm` stub decides each removal. The positional arguments the stubs read
# are `-c <uri> <subcommand> [<domain>]`.
#
# `command rm` in the undefine branch, not bare `rm`: the overlay arm replaces `rm` with a stub
# that refuses, and a domain teardown is not what that arm is about.
_REAP_STUBS = (
    'sudo() { "$@"; }\n'
    "virsh() {\n"
    '  case "$3" in\n'
    '  list) ls "$defined" ;;\n'
    '  undefine) command rm -f "${defined}/$4" ;;\n'
    "  esac\n"
    "  return 0\n"
    "}\n"
)


def _counting_virsh(fail_list_from: int) -> str:
    """A `virsh` whose `list` succeeds until the `fail_list_from`-th call, then fails.

    `stack-down.sh` enumerates three times on a full `--wipe`: once at the up-front gate, once to
    find the domains to reap, once to read the end state back. Counting through a file rather than
    a shell variable is required, not stylistic -- every call is made inside a command
    substitution, so an incremented variable dies with the subshell.
    """
    return (
        "virsh() {\n"
        '  case "$3" in\n'
        "  list)\n"
        '    echo x >>"${listcalls}"\n'
        f'    if (($(wc -l <"${{listcalls}}") >= {fail_list_from})); then\n'
        '      echo "error: failed to connect to the hypervisor" >&2\n'
        "      return 1\n"
        "    fi\n"
        '    ls "$defined"\n'
        "    ;;\n"
        '  undefine) command rm -f "${defined}/$4" ;;\n'
        "  esac\n"
        "  return 0\n"
        "}\n"
    )


def _recording_sudo(log: Path) -> str:
    """A `sudo` that records the command line it was handed and then runs it.

    Appended *after* `_REAP_STUBS`, so it replaces that module's pass-through rather than
    sitting beside it. It still runs the command, which is what keeps the reap's exit code an
    assertion instead of collateral: an arm that recorded escalations by refusing them would
    prove which calls asked for root and nothing at all about whether the reap still works.

    `echo` writes to `log` through its own redirection, so the record survives the
    `>/dev/null 2>&1` and `2>&1 >/dev/null` the reap wraps three of its four calls in.
    """
    return f'sudo() {{ echo "$*" >>"{log}"; "$@"; }}\n'


def _system_daemon_closed_to_the_operator(log: Path) -> str:
    """A host where the system daemon answers root and refuses the invoking account.

    This is the provisioned-host shape: the lifecycle contract publishes a per-uid session socket
    precisely so operator and worker accounts stay out of root's daemon, so `virsh -c
    qemu:///system` is a permission error for them and succeeds only under `sudo`. The stub
    distinguishes the two by having `sudo` mark the call it is running, which is the only thing
    separating them here -- everything else behaves as `_REAP_STUBS`.
    """
    return (
        f'sudo() {{ echo "$*" >>"{log}"; _escalated=1; "$@"; local rc=$?; _escalated=0; '
        "return $rc; }\n"
        "virsh() {\n"
        '  if [[ "${_escalated:-0}" != "1" ]]; then\n'
        '    echo "error: failed to connect to the hypervisor: Permission denied" >&2\n'
        "    return 1\n"
        "  fi\n"
        '  case "$3" in\n'
        '  list) ls "$defined" ;;\n'
        '  undefine) command rm -f "${defined}/$4" ;;\n'
        "  esac\n"
        "  return 0\n"
        "}\n"
    )


def _wipe_reap(
    tmp_path: Path,
    lib_extra: str = "",
    *,
    domains: tuple[str, ...] = ("kdive-alpha", "kdive-beta"),
    overlays: tuple[str, ...] = (),
    rootfs_mode: int | None = None,
    uri: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `stack-down.sh --wipe --yes` past the libvirt gate with a staged host.

    The contract is the readable one, so the run reaches the reap instead of being refused at
    the `--wipe` guard; `domains`, `overlays` and `lib_extra` then decide what the reap finds and
    whether its removals are permitted to succeed. `rootfs_mode` stages the overlay directory's
    permissions for the run and is restored afterwards, so a failing assertion cannot leave
    `tmp_path` unremovable.

    `uri` overrides the endpoint the run resolves. It is set in the environment rather than in
    the contract file because that is the only way to reach a *non-session* endpoint at all:
    `load_published_libvirt_uri`'s allowlist admits the two session URIs and nothing else, while
    `resolve_libvirt_uri` honours a caller-supplied value by design (ADR-0659). On a host that
    publishes a contract, an override that disagrees with it is reported and honoured
    (ADR-0661) -- stderr, never a refusal, so the run still reaches the reap.
    """
    _, staged = _published_contract(tmp_path)
    if uri is not None:
        staged["KDIVE_LIBVIRT_URI"] = uri
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    for name in overlays:
        (rootfs / name).write_text("qcow2", encoding="utf-8")
    defined = tmp_path / "defined"
    defined.mkdir()
    for name in domains:
        (defined / name).touch()
    # Created here, never truncated in the stub: `worker-lifecycle.sh stop` sources the same
    # lib.sh, so a `: >"$listcalls"` in the preamble would reset the count mid-teardown and every
    # call ordinal after the gate would be wrong.
    (tmp_path / "listcalls").touch()
    preamble = f'defined="{defined}"\nlistcalls="{tmp_path}/listcalls"\n'
    script = _isolated_stack_down(tmp_path, tmp_path / "events", preamble + _REAP_STUBS + lib_extra)
    if rootfs_mode is not None:
        rootfs.chmod(rootfs_mode)
    try:
        return subprocess.run(
            ["bash", str(script), "--wipe", "--yes"],
            capture_output=True,
            text=True,
            check=False,
            env=staged,
        )
    finally:
        rootfs.chmod(0o700)


def test_wipe_names_every_domain_and_overlay_it_removed(tmp_path: Path) -> None:
    """#2515 acceptance 1: `--wipe` reports what it actually removed.

    The reap used to print one `destroying <domain>` line per name it *intended* to reach and
    then `done`, with every `virsh` and `rm` suffixed `|| true`. That is an intention, not a
    result: the same output appeared whether the domain went away or the call was refused. The
    lines asserted here are written after each removal succeeded, and the overlay assertion is
    made against the filesystem rather than against stdout.
    """
    result = _wipe_reap(tmp_path, overlays=("alpha-overlay.qcow2", "beta-overlay.qcow2"))
    assert result.returncode == 0, result.stderr
    assert "removed domain kdive-alpha" in result.stdout
    assert "removed domain kdive-beta" in result.stdout
    assert "removed overlay" in result.stdout
    assert list((tmp_path / "rootfs").iterdir()) == []
    assert result.stdout.rstrip().endswith("done")


def test_wipe_names_the_endpoint_a_zero_domain_report_came_from(tmp_path: Path) -> None:
    """A host with no kdive domains and no overlays is a legitimate success, so it must not be
    reported as a failure -- but it must not be reported as a clean host either.

    An endpoint that answers with an empty list is either a clean host or the wrong daemon of
    the two `libvirt-uri.sh` publishes, holding no kdive domains while the real ones survive on
    the other. The liveness probe cannot tell those apart -- it grades connectivity, not identity
    -- and neither can anything else available locally. Naming the endpoint every zero came from
    is the whole of what this script can honestly offer, so `reaped 0 item(s)` is never readable
    on its own as "this host is clean".
    """
    result = _wipe_reap(tmp_path, domains=())
    assert result.returncode == 0, result.stderr
    assert "removed domain" not in result.stdout
    assert "reaped 0 item(s)" in result.stdout
    assert f"no kdive domains at {_PUBLISHED_URI}" in result.stdout
    assert f"reaped 0 item(s) at {_PUBLISHED_URI}" in result.stdout


def test_wipe_exits_non_zero_and_names_a_domain_it_could_not_undefine(tmp_path: Path) -> None:
    """#2515 acceptance 2: a reap that could not remove something exits non-zero.

    `destroy` keeps its suppression -- a domain that is already shut off says so and that is not
    a reap failure -- which this arm holds by refusing only the `undefine` for one of the two
    domains. The refused one stays defined, so the end-state re-read reports it as surviving and
    carries virsh's own diagnostic with it.
    """
    refuse_beta = (
        "virsh() {\n"
        '  if [[ "$3" == undefine && "$4" == kdive-beta ]]; then\n'
        '    echo "error: Failed to undefine domain kdive-beta: authentication failed" >&2\n'
        "    return 1\n"
        "  fi\n"
        '  case "$3" in\n'
        '  list) ls "$defined" ;;\n'
        '  undefine) command rm -f "${defined}/$4" ;;\n'
        "  esac\n"
        "  return 0\n"
        "}\n"
    )
    result = _wipe_reap(tmp_path, refuse_beta)
    assert result.returncode != 0
    assert "removed domain kdive-alpha" in result.stdout
    assert "kdive-beta" in result.stderr
    # Verbatim, because the operator's next move depends on which refusal this was.
    assert "Failed to undefine domain kdive-beta: authentication failed" in result.stderr
    # `done` is the whole defect: it is what told the operator the host had been wiped.
    assert not result.stdout.rstrip().endswith("done")


def test_wipe_will_not_call_a_still_defined_domain_removed(tmp_path: Path) -> None:
    """`virsh undefine` on a *running* domain succeeds by converting it to a transient one
    without stopping it, so neither call's exit status proves the domain is gone.

    This arm answers both `destroy` and `undefine` with 0 while leaving the domains defined --
    the shape a refused `destroy` followed by an accepted `undefine` produces. Grading on the
    calls alone would report two removals; grading on the re-read end state does not.
    """
    still_defined = 'virsh() { case "$3" in list) ls "$defined" ;; esac; return 0; }\n'
    result = _wipe_reap(tmp_path, still_defined, overlays=("alpha-overlay.qcow2",))
    assert result.returncode != 0
    assert "removed domain" not in result.stdout
    assert "still defined after destroy + undefine" in result.stderr
    assert "kdive-alpha" in result.stderr and "kdive-beta" in result.stderr


def test_wipe_refuses_a_dead_endpoint_before_stopping_or_dropping_anything(
    tmp_path: Path,
) -> None:
    """`require_libvirt_uri` proves the contract RESOLVED; a daemon stopped behind a perfectly
    valid contract passes it. Discovering that at the reap is too late -- `docker compose down -v`
    has already run, so the data volumes are gone and the domains are not.

    That is the half-wipe the up-front `--wipe` gate exists to prevent and the shape ADR-0659
    prescribes refusing wholesale, so liveness is proved at the gate. The empty event log is the
    assertion that carries it: not a banner absent from stdout, but no teardown step having run.
    """
    unreachable = 'virsh() { echo "error: failed to connect to the hypervisor" >&2; return 1; }\n'
    result = _wipe_reap(tmp_path, unreachable, overlays=("alpha-overlay.qcow2",))
    assert result.returncode != 0
    assert not (tmp_path / "events").exists(), (tmp_path / "events").read_text(encoding="utf-8")
    assert "=== stopping host processes ===" not in result.stdout
    assert "error: failed to connect to the hypervisor" in result.stderr
    assert "nothing has been stopped or dropped" in result.stderr
    assert (tmp_path / "rootfs" / "alpha-overlay.qcow2").exists()


def test_wipe_reports_an_enumeration_that_fails_after_the_gate(tmp_path: Path) -> None:
    """The gate proves liveness at gate time, not at reap time, so the reap keeps its own check.

    A daemon lost between the two lands here. `kdive_domains` would have discarded virsh's stderr
    and status and ended in `|| true`, making this byte-identical to a host with no kdive domains
    -- #2515's Effect paragraph by a second route, and made worse, because every overlay would be
    deleted while every domain survived.
    """
    result = _wipe_reap(
        tmp_path, _counting_virsh(fail_list_from=2), overlays=("alpha-overlay.qcow2",)
    )
    assert result.returncode != 0
    assert "cannot enumerate" in result.stderr
    assert "error: failed to connect to the hypervisor" in result.stderr
    assert not result.stdout.rstrip().endswith("done")
    # Half a wipe is worse than none: the surviving domains keep their backing disks.
    assert (tmp_path / "rootfs" / "alpha-overlay.qcow2").exists()
    # The volume drop is irreversible and already ran, so a failure list that does not mention it
    # leaves the operator to infer the database survived.
    assert "compose data volumes were already dropped" in result.stderr


def test_wipe_will_not_report_a_removal_it_could_not_verify(tmp_path: Path) -> None:
    """The third enumeration is the one that grades the reap, so losing the daemon before it means
    no removal can be confirmed -- and the conservative fallback treats every enumerated domain as
    surviving rather than as removed.

    Without that fallback the end-state list would be empty, no domain would match it, and all of
    them would be graded `removed` -- a false success reached through the failure handling itself.
    """
    result = _wipe_reap(
        tmp_path, _counting_virsh(fail_list_from=3), overlays=("alpha-overlay.qcow2",)
    )
    assert result.returncode != 0
    assert "removed domain" not in result.stdout
    assert "unverifiable: the end state could not be re-read" in result.stderr
    assert "error: failed to connect to the hypervisor" in result.stderr
    assert "kdive-alpha" in result.stderr and "kdive-beta" in result.stderr
    assert (tmp_path / "rootfs" / "alpha-overlay.qcow2").exists()


def test_wipe_exits_non_zero_and_names_an_overlay_it_could_not_remove(tmp_path: Path) -> None:
    """The overlay half of acceptance 2. `rm -f` reports a refused unlink and exits non-zero;
    the `|| true` that used to follow it discarded exactly that."""
    result = _wipe_reap(
        tmp_path,
        'rm() { echo "rm: cannot remove overlay: Permission denied" >&2; return 1; }\n',
        overlays=("alpha-overlay.qcow2",),
    )
    assert result.returncode != 0
    assert "alpha-overlay.qcow2" in result.stderr
    assert "rm: cannot remove overlay: Permission denied" in result.stderr
    assert "still present after rm" in result.stderr
    assert not result.stdout.rstrip().endswith("done")


def test_wipe_grades_an_overlay_on_the_end_state_not_on_rm_s_exit_status(tmp_path: Path) -> None:
    """The domain half re-reads the end state because neither `destroy` nor `undefine` returning 0
    proves the domain is gone. The overlay half owes the same: a `removed overlay` line claims the
    file is gone, so it is written from the filesystem rather than from `rm`'s exit status.

    An `rm` that exits 0 without unlinking is not something a real `sudo rm -f` does -- permission
    denied, EROFS and an immutable attribute all exit non-zero even under `-f`. The arm exists to
    hold the stated rule, and to keep the README sentence that promises it true.
    """
    result = _wipe_reap(tmp_path, "rm() { return 0; }\n", overlays=("alpha-overlay.qcow2",))
    assert result.returncode != 0
    assert "removed overlay" not in result.stdout
    assert "still present after rm" in result.stderr
    assert (tmp_path / "rootfs" / "alpha-overlay.qcow2").exists()


def test_wipe_warns_when_it_removes_overlays_an_empty_endpoint_disclaims(tmp_path: Path) -> None:
    """Zero domains plus overlays on disk is the one contradiction available locally, and it is
    the signature of a wrong-daemon URI -- which `libvirt-uri.sh`'s own repair guidance can hand
    an operator. There the domains are alive on another daemon and have just lost their disks,
    while `no kdive domains at <URI>` reads as "there was nothing to remove".

    It warns rather than refusing: a host cleaned in a previous pass looks identical, and sweeping
    genuinely orphaned overlays is a purpose of `--wipe`, so refusing would trade a silent wrong
    outcome for a loud one. The exit stays 0 and the sweep still happens.
    """
    result = _wipe_reap(tmp_path, domains=(), overlays=("alpha-overlay.qcow2",))
    assert result.returncode == 0, result.stderr
    assert "removed overlay" in result.stdout
    assert not (tmp_path / "rootfs" / "alpha-overlay.qcow2").exists()
    assert "WARNING: removed 1 overlay(s)" in result.stderr
    assert "reported zero" in result.stderr
    assert "without their disks" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory regardless of mode")
def test_wipe_refuses_to_read_an_unlistable_overlay_directory_as_empty(tmp_path: Path) -> None:
    """The overlay glob is the *shell's*, so it is expanded with the caller's own privilege.

    On an account that cannot list the overlay directory it expands to nothing, which is
    byte-identical to a host that has no overlays -- the silent no-op #2515 is about, in the
    one place a removal failure cannot surface it because no removal is ever attempted.
    """
    result = _wipe_reap(
        tmp_path,
        domains=(),
        overlays=("alpha-overlay.qcow2",),
        rootfs_mode=0o000,
    )
    assert result.returncode != 0
    assert str(tmp_path / "rootfs") in result.stderr
    assert "not listable" in result.stderr
    # A refusal that names no way forward is a worse operator experience than the no-op was.
    assert "re-run as the account that owns it" in result.stderr
    assert not result.stdout.rstrip().endswith("done")


def test_wipe_reaps_a_session_endpoint_without_sudo(tmp_path: Path) -> None:
    """#2516 acceptance 1, ADR-0662: on a session endpoint the reap escalates nowhere.

    The published endpoint is an operator-owned session socket, and the installer writes it
    `operator_uid:group_gid:770` beside `/var/lib/kdive/rootfs` at `operator:kdive-live-libvirt`
    `2770`. Root owns neither, so `sudo` there bypasses the ownership gate instead of satisfying
    it -- and on a plain `qemu:///session` it reaches root's own per-uid daemon, which is a
    different host altogether from the one the operator was looking at.

    The assertion is over the *whole run*, not over the removals: the `--wipe` gate's
    enumeration, the reap's enumeration, the end-state re-read, `destroy`, `undefine` and the
    overlay `rm` all have to come in under one identity, so an escalation anywhere in the run is
    the defect. A log file that does not exist is what says none of them asked for root.
    """
    log = tmp_path / "escalations"
    result = _wipe_reap(tmp_path, _recording_sudo(log), overlays=("alpha-overlay.qcow2",))
    assert result.returncode == 0, result.stderr
    assert not log.exists(), log.read_text(encoding="utf-8")
    # The reap still has to have happened: an arm asserting only the absence of `sudo` would
    # pass just as well against a reap that was skipped entirely.
    assert "removed domain kdive-alpha" in result.stdout
    assert list((tmp_path / "rootfs").iterdir()) == []


def test_wipe_reaps_a_system_endpoint_under_sudo(tmp_path: Path) -> None:
    """The other direction, and the reason the fix is not an unconditional `sudo` removal.

    `resolve_libvirt_uri` still resolves root-owned `qemu:///system` on a host with no lifecycle
    contract, so dropping escalation everywhere would break the reap on exactly the bare dev host
    it works on today. Every one of the four reap commands keeps `sudo` there.

    The `list` assertion is the half #2515 left open and named #2516 for: the enumeration used to
    run bare while the mutations escalated, so the list that *graded* a reap could come from a
    different daemon than the one the removal *mutated*. Asserting it here is what holds the two
    together.
    """
    log = tmp_path / "escalations"
    result = _wipe_reap(
        tmp_path,
        _recording_sudo(log),
        overlays=("alpha-overlay.qcow2",),
        uri="qemu:///system",
    )
    assert result.returncode == 0, result.stderr
    escalated = log.read_text(encoding="utf-8")
    assert "virsh -c qemu:///system list --all --name" in escalated, escalated
    assert "virsh -c qemu:///system destroy kdive-alpha" in escalated, escalated
    assert "virsh -c qemu:///system undefine kdive-alpha" in escalated, escalated
    assert f"rm -f {tmp_path}/rootfs/alpha-overlay.qcow2" in escalated, escalated


def test_wipe_does_not_escalate_a_session_endpoint_carrying_a_fragment(tmp_path: Path) -> None:
    """A `#fragment` must not push a session endpoint onto the escalating branch.

    The classifier matches `*/session` anchored at end-of-string, so anything trailing the path
    defeats it. The query is stripped because the published URIs carry `socket=` there; a fragment
    has to go with it for the same reason, and leaving it on is the harmful direction of the
    misclassification -- it aims root at an operator-owned daemon, which is exactly what deriving
    the privilege from the endpoint exists to prevent.

    Asserted as the absence of any escalation across the whole run, the same way the plain session
    arm is: a log file that was never created is what says no call asked for root.
    """
    log = tmp_path / "escalations"
    result = _wipe_reap(
        tmp_path,
        _recording_sudo(log),
        overlays=("alpha-overlay.qcow2",),
        uri="qemu:///session#frag",
    )
    assert result.returncode == 0, result.stderr
    assert not log.exists(), log.read_text(encoding="utf-8")
    assert "removed domain kdive-alpha" in result.stdout
    assert list((tmp_path / "rootfs").iterdir()) == []


def test_wipe_refuses_an_endpoint_the_operator_cannot_reach_even_when_sudo_can(
    tmp_path: Path,
) -> None:
    """The gate's probe is the operator's own authorization, so `sudo` must not answer it for them.

    ADR-0662 gives the reap one privilege derived from the endpoint, but its decision governs *the
    enumeration that grades the reap* -- and the up-front gate's probe grades nothing. Routing the
    probe through that privilege too would escalate it on every non-session endpoint, and `sudo
    virsh` always connects, so the refusal would be gone.

    The case that makes it matter is the provisioned host, which is the deployment the lifecycle
    contract exists for: the published endpoint is a per-uid session socket and the operator is
    deliberately kept out of root's daemon. An operator who overrides the endpoint to
    `qemu:///system` -- which `libvirt-uri.sh` itself suggests as a repair -- is aiming at a daemon
    holding none of their domains. Unescalated, that probe is a permission error and the run stops
    here. Escalated, it would succeed, the volumes would be dropped, the endpoint would honestly
    report zero domains, and every overlay would be swept out from under domains still running on
    the session daemon -- with the run printing `done` and exiting 0.

    The empty event log is the assertion that carries it: not a banner absent from stdout, but no
    teardown step having run.
    """
    log = tmp_path / "escalations"
    result = _wipe_reap(
        tmp_path,
        _system_daemon_closed_to_the_operator(log),
        overlays=("alpha-overlay.qcow2",),
        uri="qemu:///system",
    )
    assert result.returncode != 0
    assert not (tmp_path / "events").exists(), (tmp_path / "events").read_text(encoding="utf-8")
    assert "=== stopping host processes ===" not in result.stdout
    assert "Permission denied" in result.stderr, result.stderr
    assert "nothing has been stopped or dropped" in result.stderr
    # The overlays are the thing an escalated probe would have swept, so assert they survived.
    assert (tmp_path / "rootfs" / "alpha-overlay.qcow2").exists()
    # And the run never reached a call that would have escalated: the gate refused first.
    assert not log.exists(), log.read_text(encoding="utf-8")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a 0500 directory regardless of mode")
def test_wipe_refuses_an_unwritable_overlay_directory_before_dropping_the_volumes(
    tmp_path: Path,
) -> None:
    """ADR-0662 moved the overlay `rm` onto the invoking account, so listable is no longer enough.

    The `! -r || ! -x` refusal beside the sweep was written when the removal was root's and could
    not be denied. Unlinking needs *write* on the directory, which neither of those tests covers,
    and `0500` is the mode that separates them: the glob still expands, so without this gate the
    run reaches the removals and every one of them is refused.

    Reachable without an exotic host: `stack-services.sh` runs
    `sudo install -d -o "$(id -un)" -m 0755` on this same path when it is not already writable, so
    an account other than the operator running bring-up leaves the installer's mode-`2770`
    directory as a `0755` one the operator can no longer write.

    Asserted where it matters, which is *when* the refusal lands rather than that it lands at all.
    The sweep runs after `docker compose --profile obs down -v`, so a refusal there would arrive
    with the data volumes already gone and every overlay still present -- the half-wipe the
    up-front gate exists to refuse wholesale. The empty event log is what carries that: not a
    banner absent from stdout, but no teardown step having run.
    """
    result = _wipe_reap(
        tmp_path,
        domains=(),
        overlays=("alpha-overlay.qcow2",),
        rootfs_mode=0o500,
    )
    assert result.returncode != 0
    assert not (tmp_path / "events").exists(), (tmp_path / "events").read_text(encoding="utf-8")
    assert "=== stopping host processes ===" not in result.stdout
    assert "not writable" in result.stderr, result.stderr
    assert str(tmp_path / "rootfs") in result.stderr
    # A refusal that names no way forward is a worse operator experience than the denied rm was.
    assert "re-run as the account that owns it" in result.stderr
    # ...and the way forward has to be true on the shape the refusal actually names. The sweep's
    # "owner or one in its group" is advice about *listing*; on the root-owned 0755 directory this
    # gate exists for, the group has r-x and no write, so group membership unlinks nothing. The
    # qualifier is the whole difference between guidance and a wrong turn.
    assert "if the mode grants the group write" in result.stderr
    assert "nothing has been stopped or dropped" in result.stderr
    assert (tmp_path / "rootfs" / "alpha-overlay.qcow2").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads and writes a 0400 directory regardless")
def test_wipe_refuses_a_readable_but_non_traversable_overlay_directory_at_the_gate(
    tmp_path: Path,
) -> None:
    """The glob needs READ on the directory, not traversal, so `0400` is caught here not there.

    Bash matches `*-overlay.qcow2` straight out of `readdir` without stat'ing the entries, so the
    read bit alone expands the glob. `0400` therefore reaches the gate with a non-empty match and
    an unwritable directory, and is refused before anything is stopped -- even though it would
    also have failed the sweep's `! -r || ! -x` test further down.

    Which refusal wins matters, because they do not land in the same place: the sweep's runs after
    `docker compose --profile obs down -v`. Pinning it here is what keeps the gate's comment from
    drifting back to the intuitive-but-wrong claim that anything failing `-r || -x` yields an
    empty glob; a mode with no read bit does, and this one does not.
    """
    result = _wipe_reap(
        tmp_path,
        domains=(),
        overlays=("alpha-overlay.qcow2",),
        rootfs_mode=0o400,
    )
    assert result.returncode != 0
    assert not (tmp_path / "events").exists(), (tmp_path / "events").read_text(encoding="utf-8")
    assert "not writable" in result.stderr, result.stderr
    assert "not listable" not in result.stderr, result.stderr
    assert (tmp_path / "rootfs" / "alpha-overlay.qcow2").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a 0500 directory regardless of mode")
def test_wipe_reaps_an_unwritable_but_empty_overlay_directory_cleanly(tmp_path: Path) -> None:
    """The gate is keyed on an overlay being at stake, not on the directory's mode.

    This is the arm that separates the refusal from #2515's defect, and it is not hypothetical: a
    permissions-only `! -w` test was written earlier in this change's review round and reverted
    for failing exactly here. An unwritable directory holding no overlays has nothing to remove,
    so the reap removes everything there was to remove and succeeds. Grading that as a failure is
    #2515 -- a report that does not describe the end state -- re-introduced in the opposite
    direction from the one #2515 found it in.

    The domains are left at their default so the run is a *fully successful* reap rather than a
    no-op: two domains really do go away, and the run still has to reach `done` and exit 0.
    """
    result = _wipe_reap(tmp_path, overlays=(), rootfs_mode=0o500)
    assert result.returncode == 0, result.stderr
    assert "not writable" not in result.stderr, result.stderr
    assert "removed domain kdive-alpha" in result.stdout
    assert "removed domain kdive-beta" in result.stdout
    assert "reaped 2 item(s)" in result.stdout
    assert result.stdout.rstrip().endswith("done")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a 0500 directory regardless of mode")
def test_wipe_keeps_sweeping_an_unwritable_overlay_directory_on_a_system_endpoint(
    tmp_path: Path,
) -> None:
    """The gate is branch-local: on the escalating branch root's `rm` does not need write.

    A root-owned `0755` overlay directory is the ordinary bare-host shape, so a `! -w` test
    applied to both branches would refuse a host that works today -- the same defect aimed the
    other way. The assertion is that the run reached the removal, not merely that it printed no
    refusal: an arm checking only for the absence of the message would pass just as well against
    a run that died somewhere else.

    The run's exit status is deliberately not asserted. The stub `sudo` is a pass-through, so the
    `rm` it records runs with the test account's own rights against a directory that account
    cannot write, and the overlay survives. That is the harness, not the script; the escalation
    log is the part that is evidence.
    """
    log = tmp_path / "escalations"
    result = _wipe_reap(
        tmp_path,
        _recording_sudo(log),
        domains=(),
        overlays=("alpha-overlay.qcow2",),
        rootfs_mode=0o500,
        uri="qemu:///system",
    )
    assert "not writable" not in result.stderr, result.stderr
    escalated = log.read_text(encoding="utf-8")
    assert f"rm -f {tmp_path}/rootfs/alpha-overlay.qcow2" in escalated, escalated


def _stack_status_libvirt_slice(tmp_path: Path) -> Path:
    """stack-status.sh's setup plus its libvirt section, runnable on its own.

    The idiom test_status_database_probe_scrubs_unrelated_role_dsns already uses: the shipped
    lib.sh, env.sh and libvirt-uri.sh sit beside the extract, so the declaration, the resolver,
    the guard and `provision_prereqs_ok` are all the real ones. The compose, host-daemon,
    lifecycle, app-health and database sections are cut -- none of them is what this contract is
    about, and each would drag in a backend this test does not need.
    """
    source = (ROOT / "scripts/live-stack/stack-status.sh").read_text(encoding="utf-8")
    setup = source[: source.index('echo "=== compose')]
    libvirt = source[source.index('if [[ -n "${LIBVIRT_UNRESOLVED}" ]]') :]
    status = tmp_path / "stack-status.sh"
    status.write_text(setup + libvirt, encoding="utf-8")
    for name in ("lib.sh", "env.sh", "libvirt-uri.sh"):
        (tmp_path / name).write_text(
            (ROOT / "scripts/live-stack" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    return status


def test_stack_status_reports_an_unresolved_endpoint_without_probing_it(tmp_path: Path) -> None:
    """The banner and the `libvirt_ok` probe both read the endpoint, so both sit inside the
    resolved branch; `provision_prereqs_ok` reads no libvirt and stays outside it, because the
    overlay-and-staging report is the part a broken host still needs.

    Run, not read. Asserting the source text's ordering instead passes against a script whose two
    branch bodies are swapped -- which still carries every literal in the asserted order, and
    still dies with `unbound variable` on exactly the broken-contract host this names.
    """
    contract, staged = _broken_contract(tmp_path)
    result = subprocess.run(
        ["bash", str(_stack_status_libvirt_slice(tmp_path))],
        capture_output=True,
        text=True,
        check=False,
        env=staged,
    )
    assert result.returncode == 0, result.stderr
    assert "=== libvirt (endpoint unresolved) ===" in result.stdout
    assert str(contract) in result.stdout
    assert "daemon: reachable" not in result.stdout
    assert "daemon: UNREACHABLE" not in result.stdout
    assert "provision prereqs:" in result.stdout


def test_stack_status_still_probes_a_resolved_endpoint(tmp_path: Path) -> None:
    """The degraded report is the exception, not the new default: a host whose published contract
    validates keeps the endpoint banner and the probe it guards."""
    _, staged = _published_contract(tmp_path)
    result = subprocess.run(
        ["bash", str(_stack_status_libvirt_slice(tmp_path))],
        capture_output=True,
        text=True,
        check=False,
        env=staged,
    )
    assert result.returncode == 0, result.stderr
    assert f"=== libvirt ({_PUBLISHED_URI}) ===" in result.stdout
    assert "endpoint unresolved" not in result.stdout


def test_server_and_worker_receive_the_same_libvirt_endpoint(tmp_path: Path) -> None:
    """The endpoint agreement #2480 asks for, asserted as agreement rather than as a substring
    of either script: the server-side value is read out of the environment
    ``restart_host_processes`` actually forks with, and the worker-side value comes from
    ``load_published_libvirt_uri`` -- the same call ``worker-lifecycle.sh request start`` makes
    to fill ``KDIVE_LIFECYCLE_LIBVIRT_URI`` in the worker's unit environment."""
    _, staged = _published_contract(tmp_path)
    lifecycle = tmp_path / "scripts/live-stack"
    lifecycle.mkdir(parents=True)
    (lifecycle / "worker-lifecycle.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (lifecycle / "worker-lifecycle.sh").chmod(0o755)
    server_side = tmp_path / "server-endpoint"
    worker_side = tmp_path / "worker-endpoint"
    python = tmp_path / "python"
    python.write_text(
        '#!/bin/sh\ncase "$*" in *server*) printf \'%s\' "${KDIVE_LIBVIRT_URI-unset}"'
        ' > "$KDIVE_DAEMON_PROBE" ;; esac\n',
        encoding="utf-8",
    )
    python.chmod(0o755)

    staged.update(KDIVE_DAEMON_PROBE=str(server_side), KDIVE_STATUS_PROBE=str(worker_side))
    result = _sourced(
        ROOT / "scripts/live-stack/lib.sh",
        f'repo_root="{tmp_path}"\n'
        f'py="{python}"\n'
        f'log_dir="{tmp_path / "logs"}"\n'
        "stop_daemons() { :; }\n"
        "require_free_http_port() { :; }\n"
        "wait_for_daemons_to_settle() { :; }\n"
        "restart_host_processes\n"
        'printf %s "$(load_published_libvirt_uri)" > "$KDIVE_STATUS_PROBE"\n',
        staged,
    )
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not server_side.exists():
        time.sleep(0.05)
    assert server_side.read_text(encoding="utf-8") == worker_side.read_text(encoding="utf-8")
    assert worker_side.read_text(encoding="utf-8") == _PUBLISHED_URI


def test_libvirt_uri_parser_is_safe_to_source_twice() -> None:
    """lib.sh, env.sh and worker-lifecycle.sh each source this file, and the example sources the
    second through the first, so more than one source per shell is ordinary. `readonly` made the
    second one abort the shell with "readonly variable" before the body ran."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail\nsource "{LIBVIRT_URI}"\nsource "{LIBVIRT_URI}"\n'
            'printf %s "$LIBVIRT_ENV"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "/etc/kdive/live-worker-libvirt.env"
    assert "readonly" not in result.stderr


def test_local_libvirt_example_env_resolves_the_published_endpoint(tmp_path: Path) -> None:
    """The example wrapper keeps working once the live-stack env owns the resolution: it must
    still source cleanly under `set -euo pipefail` (demo-up.sh sources it first and does nothing
    otherwise) and still reach its own values past the shared block."""
    _, staged = _published_contract(tmp_path)
    staged.pop("KDIVE_PROJECT", None)
    result = _sourced(
        ROOT / "examples/local-libvirt/env.sh",
        'bash -c \'printf "%s|%s" "${KDIVE_LIBVIRT_URI-unset}" "${KDIVE_PROJECT-unset}"\'',
        staged,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_PUBLISHED_URI}|demo"


def test_client_urls_derive_from_the_configurable_ports() -> None:
    # The port var must be the SINGLE source of truth: the client-facing DSN/endpoint defaults must
    # reference the port var, not a second hardcoded literal that could silently drift from compose.
    env = (ROOT / "scripts/live-stack/env.sh").read_text()
    assert "localhost:${KDIVE_POSTGRES_PORT}/kdive" in env
    assert "http://localhost:${KDIVE_SEAWEEDFS_PORT}" in env
    assert "http://localhost:${KDIVE_OIDC_PORT}/default" in env


def test_live_stack_scripts_are_strict_bash() -> None:
    for name in (
        "env.sh",
        "apply-migrations.sh",
        "stack-services.sh",
        "stack-down.sh",
        "stack-status.sh",
        "provision-queue-diagnostics.sh",
    ):
        text = (ROOT / "scripts/live-stack" / name).read_text()
        assert text.startswith("#!/usr/bin/env bash\n"), f"{name}: missing bash shebang"
        assert "\nset -euo pipefail\n" in text, f"{name}: missing 'set -euo pipefail'"


def test_provision_queue_diagnostics_is_exact_bounded_and_redacted() -> None:
    text = (ROOT / "scripts/live-stack/provision-queue-diagnostics.sh").read_text()

    assert "KDIVE_SERVER_DATABASE_URL" in text
    assert "connect_timeout=5" in text
    assert "SET TRANSACTION READ ONLY" in text
    assert "SET LOCAL statement_timeout = '5s'" in text
    assert "j.id = %s AND j.kind = 'provision' AND s.id = %s" in text
    assert "last_heartbeat_at" in text
    assert "provision-evidence-error code=" in text
    for forbidden in ("j.payload,", "authorizing", "failure_context", "print(exc", "traceback"):
        assert forbidden not in text


def test_provision_queue_diagnostics_sources_env_before_reporting_malformed_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.write_text("malformed\n")

    result = subprocess.run(
        [str(ROOT / "scripts/live-stack/provision-queue-diagnostics.sh"), str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 4
    assert result.stdout == ""
    assert result.stderr == "provision-evidence-error code=target-malformed\n"


def test_provision_queue_diagnostics_rejects_target_without_final_lf(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.write_text(
        "11111111-1111-1111-1111-111111111111\t22222222-2222-2222-2222-222222222222",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(ROOT / "scripts/live-stack/provision-queue-diagnostics.sh"), str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 4
    assert result.stdout == ""
    assert result.stderr == "provision-evidence-error code=target-malformed\n"


def _limit_provision_diagnostic_address_space() -> None:
    limit = 256 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def test_provision_queue_diagnostics_rejects_oversized_target_without_reading_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    with target.open("wb") as output:
        output.seek(1024 * 1024 * 1024)
        output.write(b"\n")

    result = subprocess.run(
        [str(ROOT / "scripts/live-stack/provision-queue-diagnostics.sh"), str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
        preexec_fn=_limit_provision_diagnostic_address_space,
    )

    assert result.returncode == 4
    assert result.stdout == ""
    assert result.stderr == "provision-evidence-error code=target-malformed\n"


@pytest.mark.parametrize(
    "job_id",
    [
        "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "{11111111-1111-1111-1111-111111111111}",
        "11111111111111111111111111111111",
    ],
)
def test_provision_queue_diagnostics_rejects_noncanonical_target(
    tmp_path: Path, job_id: str
) -> None:
    target = tmp_path / "target"
    target.write_text(
        f"{job_id}\t22222222-2222-2222-2222-222222222222\n",
        encoding="ascii",
    )

    result = subprocess.run(
        [str(ROOT / "scripts/live-stack/provision-queue-diagnostics.sh"), str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 4
    assert result.stdout == ""
    assert result.stderr == "provision-evidence-error code=target-malformed\n"


def test_provision_queue_diagnostics_rejects_symlink_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text(
        "11111111-1111-1111-1111-111111111111\t22222222-2222-2222-2222-222222222222\n",
        encoding="ascii",
    )
    target = tmp_path / "target"
    target.symlink_to(source)

    result = subprocess.run(
        [str(ROOT / "scripts/live-stack/provision-queue-diagnostics.sh"), str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 4
    assert result.stdout == ""
    assert result.stderr == "provision-evidence-error code=target-malformed\n"


@pytest.mark.parametrize(
    "failure",
    [
        'raise ImportError("driver missing at /sensitive/path")\n',
        'print("sensitive import output"); '
        'raise RuntimeError("driver broken at /sensitive/path")\n',
    ],
)
def test_provision_queue_diagnostics_sanitizes_database_driver_import_failure(
    tmp_path: Path, failure: str
) -> None:
    (tmp_path / "psycopg.py").write_text(failure, encoding="utf-8")
    target = tmp_path / "target"
    target.write_text(
        "11111111-1111-1111-1111-111111111111\t22222222-2222-2222-2222-222222222222\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(ROOT / "scripts/live-stack/provision-queue-diagnostics.sh"), str(target)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 5
    assert result.stdout == ""
    assert result.stderr == "provision-evidence-error code=query-unavailable\n"


def test_provision_queue_diagnostics_sanitizes_environment_source_failure(
    tmp_path: Path,
) -> None:
    script = tmp_path / "provision-queue-diagnostics.sh"
    shutil.copy2(ROOT / "scripts/live-stack/provision-queue-diagnostics.sh", script)
    (tmp_path / "env.sh").write_text(
        'printf "sensitive source output /private/path\\n"\n'
        'printf "sensitive source error /private/path\\n" >&2\n'
        "return 97\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(script), str(tmp_path / "target")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 8
    assert result.stdout == ""
    assert result.stderr == "provision-evidence-error code=diagnostic-failed\n"


def test_provision_queue_diagnostics_sanitizes_interpreter_launch_failure(
    tmp_path: Path,
) -> None:
    python = tmp_path / "python"
    python.write_text("#!/missing/interpreter\n", encoding="utf-8")
    python.chmod(0o755)
    target = tmp_path / "target"
    target.write_text(
        "11111111-1111-1111-1111-111111111111\t22222222-2222-2222-2222-222222222222\n",
        encoding="ascii",
    )

    result = subprocess.run(
        [str(ROOT / "scripts/live-stack/provision-queue-diagnostics.sh"), str(target)],
        cwd=ROOT,
        env={**os.environ, "KDIVE_PYTHON": str(python)},
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 8
    assert result.stdout == ""
    assert result.stderr == "provision-evidence-error code=diagnostic-failed\n"


def _run_provision_queue_diagnostics(
    tmp_path: Path,
    *,
    target_job_id: str,
    target_system_id: str,
    expected_job_id: str,
    expected_system_id: str,
    rows: list[list[object | None]],
) -> subprocess.CompletedProcess[str]:
    """Run the snapshot against a process-local psycopg boundary double."""
    (tmp_path / "psycopg.py").write_text(
        """
import json
from datetime import datetime
import os


class Error(Exception):
    pass


class Connection:
    def __init__(self):
        self.rows = json.loads(os.environ["PSYCOPG_ROWS"])
        self.setup = iter((
            "SET TRANSACTION READ ONLY",
            "SET LOCAL statement_timeout = '5s'",
        ))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def transaction(self):
        return self

    def execute(self, statement, params=None):
        if params is None:
            assert statement == next(self.setup)
            return self
        assert tuple(map(str, params)) == (
            os.environ["PSYCOPG_TARGET_JOB_ID"],
            os.environ["PSYCOPG_TARGET_SYSTEM_ID"],
        )
        try:
            next(self.setup)
        except StopIteration:
            pass
        else:
            raise AssertionError("query ran before read-only timeout setup")
        if tuple(map(str, params)) != (
            os.environ["PSYCOPG_EXPECTED_JOB_ID"],
            os.environ["PSYCOPG_EXPECTED_SYSTEM_ID"],
        ):
            self.rows = []
        return self

    def fetchall(self):
        return [
            [
                datetime.fromisoformat(value["__datetime__"])
                if isinstance(value, dict) and set(value) == {"__datetime__"}
                else value
                for value in row
            ]
            for row in self.rows
        ]


def connect(dsn, *, connect_timeout):
    assert dsn == "postgresql://diagnostics.invalid/kdive"
    assert connect_timeout == 5
    return Connection()
""".lstrip(),
        encoding="utf-8",
    )
    target = tmp_path / "target"
    target.write_text(f"{target_job_id}\t{target_system_id}\n", encoding="utf-8")
    encoded_rows = [
        [
            {"__datetime__": value} if index in {7, 8, 9} and isinstance(value, str) else value
            for index, value in enumerate(row)
        ]
        for row in rows
    ]
    return subprocess.run(
        [str(ROOT / "scripts/live-stack/provision-queue-diagnostics.sh"), str(target)],
        cwd=ROOT,
        env={
            **os.environ,
            "KDIVE_PYTHON": sys.executable,
            "KDIVE_SERVER_DATABASE_URL": "postgresql://diagnostics.invalid/kdive",
            "PSYCOPG_ROWS": json.dumps(encoded_rows),
            "PSYCOPG_TARGET_JOB_ID": target_job_id,
            "PSYCOPG_TARGET_SYSTEM_ID": target_system_id,
            "PSYCOPG_EXPECTED_JOB_ID": expected_job_id,
            "PSYCOPG_EXPECTED_SYSTEM_ID": expected_system_id,
            "PYTHONPATH": str(tmp_path),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def test_provision_queue_diagnostics_prints_exact_complete_tsv(tmp_path: Path) -> None:
    job_id = "11111111-1111-1111-1111-111111111111"
    system_id = "22222222-2222-2222-2222-222222222222"
    row: list[object | None] = [
        system_id,
        "provisioning",
        job_id,
        "default",
        "running",
        3,
        None,
        "2026-08-26T12:00:00+00:00",
        "2026-08-26T12:00:02+00:00",
        None,
    ]

    result = _run_provision_queue_diagnostics(
        tmp_path,
        target_job_id=job_id,
        target_system_id=system_id,
        expected_job_id=job_id,
        expected_system_id=system_id,
        rows=[row],
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "system_id\tsystem_state\tjob_id\tdispatch_lane\tjob_state\tattempt\tworker_id\t"
        "enqueued_at\tlast_heartbeat_at\tlease_expires_at\n"
        "sys-R1\tprovisioning\tjob-R1\tdefault\trunning\t3\tNONE\t"
        "2026-08-26T12:00:00+00:00\t2026-08-26T12:00:02+00:00\tNONE\n"
    )
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("field_index", "unsafe_value"),
    [
        pytest.param(1, "unknown-state", id="system-state"),
        pytest.param(3, "default\t/sensitive/path", id="dispatch-lane-control"),
        pytest.param(5, 2**31, id="attempt-range"),
        pytest.param(6, "/sensitive/worker", id="worker-id-path"),
        pytest.param(7, 42, id="timestamp-type"),
    ],
)
def test_provision_queue_diagnostics_rejects_unbounded_or_unsafe_result_fields(
    tmp_path: Path,
    field_index: int,
    unsafe_value: object,
) -> None:
    job_id = "11111111-1111-1111-1111-111111111111"
    system_id = "22222222-2222-2222-2222-222222222222"
    row: list[object | None] = [
        system_id,
        "provisioning",
        job_id,
        "default",
        "running",
        3,
        None,
        "2026-08-26T12:00:00+00:00",
        "2026-08-26T12:00:02+00:00",
        None,
    ]
    row[field_index] = unsafe_value

    result = _run_provision_queue_diagnostics(
        tmp_path,
        target_job_id=job_id,
        target_system_id=system_id,
        expected_job_id=job_id,
        expected_system_id=system_id,
        rows=[row],
    )

    assert result.returncode == 7
    assert result.stdout == ""
    assert result.stderr == "provision-evidence-error code=result-malformed\n"


@pytest.mark.parametrize(
    ("target_system_id", "expected_system_id", "rows"),
    [
        pytest.param(
            "33333333-3333-3333-3333-333333333333",
            "22222222-2222-2222-2222-222222222222",
            [["would", "otherwise", "match"]],
            id="mismatched-target",
        ),
        pytest.param(
            "22222222-2222-2222-2222-222222222222",
            "22222222-2222-2222-2222-222222222222",
            [],
            id="zero-rows",
        ),
        pytest.param(
            "22222222-2222-2222-2222-222222222222",
            "22222222-2222-2222-2222-222222222222",
            [["first"], ["second"]],
            id="multiple-rows",
        ),
    ],
)
def test_provision_queue_diagnostics_rejects_non_exact_results(
    tmp_path: Path,
    target_system_id: str,
    expected_system_id: str,
    rows: list[list[object | None]],
) -> None:
    job_id = "11111111-1111-1111-1111-111111111111"

    result = _run_provision_queue_diagnostics(
        tmp_path,
        target_job_id=job_id,
        target_system_id=target_system_id,
        expected_job_id=job_id,
        expected_system_id=expected_system_id,
        rows=rows,
    )

    assert result.returncode == 6
    assert result.stdout == ""
    assert result.stderr == "provision-evidence-error code=target-mismatch\n"


def test_worker_journal_evidence_filter_emits_only_fixed_records() -> None:
    accepted = (
        "worker local-systemd:kdive-live-worker@1.service:"
        "0123456789abcdef0123456789abcdef accepting dispatch lanes: default,state-fenced"
    )
    claim = (
        "worker local-systemd:kdive-live-worker@1.service:"
        "0123456789abcdef0123456789abcdef claimed provision job "
        "11111111-1111-1111-1111-111111111111 lane=default attempt=0 "
        "enqueued_at=2026-08-26T14:51:00.739273+00:00 "
        "claim_at=2026-08-26T14:51:01.000000+00:00 queue_delay_s=0.260727"
    )
    provider = (
        "local-libvirt provision system=22222222-2222-2222-2222-222222222222 "
        "job=11111111-1111-1111-1111-111111111111 stage=snapshot-pre-existing event=start"
    )
    failure = (
        "worker local-systemd:kdive-live-worker@1.service:"
        "0123456789abcdef0123456789abcdef claim loop failure "
        "lane=default reason=postgres-57P01"
    )
    poisoned = f"{claim} secret=/sensitive/path"
    payload = "\n".join(
        (
            json.dumps({"msg": accepted}),
            "Traceback: /opt/kdive/private.py",
            json.dumps({"msg": claim}),
            json.dumps({"msg": poisoned}),
            json.dumps({"msg": provider}),
            json.dumps({"msg": failure}),
            "",
        )
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/live-stack/filter-worker-journal-evidence.py")],
        cwd=ROOT,
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == f"{accepted}\n{claim}\n{provider}\n{failure}\n"


@pytest.mark.parametrize(
    "reason",
    ("pool-timeout", "timeout", "postgres-57P01", "postgres-unknown", "unexpected"),
)
def test_worker_journal_evidence_filter_accepts_fixed_claim_loop_reasons(reason: str) -> None:
    message = (
        "worker local-systemd:kdive-live-worker@1.service:"
        "0123456789abcdef0123456789abcdef claim loop failure "
        f"lane=default reason={reason}"
    )

    result = _run_worker_journal_filter(json.dumps({"msg": message}).encode() + b"\n")

    assert result.returncode == 0
    assert result.stdout == f"{message}\n".encode()
    assert result.stderr == b""


@pytest.mark.parametrize(
    "message",
    (
        "worker local-systemd:kdive-live-worker@1.service:"
        "0123456789abcdef0123456789abcdef claim loop failure "
        "lane=default reason=pool_timeout",
        "worker local-systemd:kdive-live-worker@1.service:"
        "0123456789abcdef0123456789abcdef claim loop failure "
        "lane=default reason=postgres-57P01 secret=/private/path",
        "run_once failed on lane default; continuing after 1.0s: /private/path",
    ),
)
def test_worker_journal_evidence_filter_rejects_non_fixed_claim_loop_records(
    message: str,
) -> None:
    result = _run_worker_journal_filter(json.dumps({"msg": message}).encode() + b"\n")

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("enqueued_at", "NONE", id="missing-enqueued-at"),
        pytest.param("claim_at", "NONE", id="missing-claim-at"),
        pytest.param("enqueued_at", "2026-08-26T14:51:00", id="naive-enqueued-at"),
        pytest.param("claim_at", "2026-08-26T14:51:01", id="naive-claim-at"),
    ],
)
def test_worker_journal_evidence_filter_rejects_claim_without_authoritative_timestamps(
    field: str, value: str
) -> None:
    timestamps = {
        "enqueued_at": "2026-08-26T14:51:00.739273+00:00",
        "claim_at": "2026-08-26T14:51:01.000000+00:00",
    }
    timestamps[field] = value
    claim = (
        "worker local-systemd:kdive-live-worker@1.service:"
        "0123456789abcdef0123456789abcdef claimed provision job "
        "11111111-1111-1111-1111-111111111111 lane=default attempt=0 "
        f"enqueued_at={timestamps['enqueued_at']} claim_at={timestamps['claim_at']} "
        "queue_delay_s=0.260727"
    )

    result = _run_worker_journal_filter(json.dumps({"msg": claim}).encode() + b"\n")

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b""


def test_worker_journal_evidence_filter_fails_when_no_safe_record_exists() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/live-stack/filter-worker-journal-evidence.py")],
        cwd=ROOT,
        input='{"msg":"Traceback: /sensitive/path"}\n',
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == ""


def _run_worker_journal_filter(payload: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/live-stack/filter-worker-journal-evidence.py")],
        cwd=ROOT,
        input=payload,
        capture_output=True,
        check=False,
        timeout=15,
    )


_SAFE_WORKER_START = (
    b'{"msg":"worker local-systemd:kdive-live-worker@1.service:'
    b'0123456789abcdef0123456789abcdef accepting dispatch lanes: default,state-fenced"}\n'
)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            _SAFE_WORKER_START + json.dumps({"msg": "x" * 5000}).encode() + b"\n",
            id="per-record-bytes",
        ),
        pytest.param(_SAFE_WORKER_START + b"{}\n" * 400, id="record-count"),
        pytest.param(
            _SAFE_WORKER_START + ((b" " * 2999) + b"\n") * 100,
            id="total-bytes",
        ),
    ],
)
def test_worker_journal_evidence_filter_rejects_input_beyond_bounds(
    payload: bytes,
) -> None:
    result = _run_worker_journal_filter(payload)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b""


def test_worker_journal_evidence_filter_rejects_unbounded_accepted_field() -> None:
    message = (
        "worker local-systemd:kdive-live-worker@1.service:"
        "0123456789abcdef0123456789abcdef accepting dispatch lanes: " + "a" * 2000
    )
    result = _run_worker_journal_filter(json.dumps({"msg": message}).encode() + b"\n")

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b""


def test_worker_readiness_evidence_filter_emits_only_component_booleans() -> None:
    payload = json.dumps(
        {
            "ready": False,
            "checks": {
                "postgres": True,
                "seaweedfs": True,
                "capture_bootstrap_manifest": False,
                "capture_recovery": True,
            },
            "version": {
                "version": "0.4.1-dev",
                "commit": "0123456789abcdef",
                "built_at": None,
            },
        }
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/live-stack/filter-worker-readiness-evidence.py")],
        cwd=ROOT,
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == (
        "worker_readiness ready=false postgres=true seaweedfs=true "
        "capture_bootstrap_manifest=false capture_recovery=true\n"
    )
    assert "version" not in result.stdout
    assert "commit" not in result.stdout


@pytest.mark.parametrize(
    "payload",
    (
        "{}",
        '{"ready":false,"checks":{"postgres":true}}',
        (
            '{"ready":false,"checks":{"postgres":true,"minio":true,'
            '"capture_bootstrap_manifest":false,"capture_recovery":true,'
            '"unexpected":"private"},"version":{}}'
        ),
        "x" * 4097,
    ),
)
def test_worker_readiness_evidence_filter_rejects_non_exact_or_oversized_input(
    payload: str,
) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/live-stack/filter-worker-readiness-evidence.py")],
        cwd=ROOT,
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "script",
    (
        "filter-worker-journal-evidence.py",
        "filter-worker-readiness-evidence.py",
    ),
)
def test_worker_evidence_filters_silence_deeply_nested_json_failure(
    script: str, tmp_path: Path
) -> None:
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\nsys.setrecursionlimit(100)\n",
        encoding="utf-8",
    )
    nested_json = "[" * 200 + "0" + "]" * 200 + "\n"

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/live-stack" / script)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        input=nested_json,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert len(nested_json.encode()) < 4096
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_restart_host_processes_starts_ordinary_daemons_and_lifecycle_workers() -> None:
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert "restart_host_processes" in text
    assert "-m kdive server" in text
    assert "-m kdive reconciler" in text
    assert 'worker-lifecycle.sh" start' in text


def test_worker_lifecycle_receives_worker_member_dsn_and_s3_endpoint() -> None:
    # sudo resets the environment, so the root worker re-sources env.sh and would re-default any
    # relocated backend port. The resolved DB + S3 endpoints must be forwarded into the sudo shell
    # so a KDIVE_POSTGRES_PORT/KDIVE_SEAWEEDFS_PORT override reaches the worker, not just the
    # same-user
    # server/reconciler. The forward must appear inside the `sudo bash -c` block.
    lifecycle = (ROOT / "scripts/live-stack/worker-lifecycle.sh").read_text()
    assert '"worker_database_url": os.environ["KDIVE_WORKER_DATABASE_URL"]' in lifecycle
    assert '"s3_endpoint_url": os.environ["KDIVE_S3_ENDPOINT_URL"]' in lifecycle


def test_grafana_gate_skips_ppc64le_and_keeps_other_arches() -> None:
    """The arch gate must skip grafana only where it has no manifest (ppc64le), not elsewhere.

    Executes the real predicate so an inverted or gutted gate fails, unlike a substring check.
    """
    assert _grafana_supports_arch("ppc64le") is False, "grafana has no ppc64le manifest (ADR-0356)"
    assert _grafana_supports_arch("x86_64") is True
    assert _grafana_supports_arch("aarch64") is True
    # An empty/unknown arch (no `uname`) must not silently skip grafana — attempt it best-effort.
    assert _grafana_supports_arch("") is True


@pytest.mark.skipif(shutil.which("ss") is None, reason="ss (iproute2) required to inspect ports")
def test_require_free_http_port_fails_when_the_port_is_held() -> None:
    """A foreign listener on KDIVE_HTTP_PORT must fail the guard with a remediation, not proceed."""
    with _listening_port() as port:
        result = _require_free_http_port(port)
    assert result.returncode != 0, "guard must fail when the port is occupied"
    assert str(port) in result.stderr
    assert "KDIVE_HTTP_PORT=8001" in result.stderr  # remediation names the override


@pytest.mark.skipif(shutil.which("ss") is None, reason="ss (iproute2) required to inspect ports")
def test_require_free_http_port_passes_when_the_port_is_free() -> None:
    result = _require_free_http_port(_free_port())
    assert result.returncode == 0, result.stderr


def test_restart_host_processes_guards_the_port_after_stopping_daemons() -> None:
    # The guard must run AFTER stop_daemons (so a kdive server we just stopped is not mis-flagged)
    # and BEFORE the server launches (so it actually prevents the lost bind race).
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    stop = text.index("\n  stop_daemons\n")
    guard = text.index("require_free_http_port || return 1")
    launch = text.index('setsid nohup "$py" -m kdive server')
    assert stop < guard < launch, "guard must sit between stop_daemons and the server launch"


def test_up_starts_prometheus_independently_of_grafana() -> None:
    """Prometheus comes up in its own `compose up`, so a grafana failure can't abort it (#1261)."""
    text = (ROOT / "scripts/live-stack/stack-services.sh").read_text()
    assert "up -d prometheus" in text, "prometheus must be brought up on its own"
    assert "grafana_supports_arch" in text, "grafana must be gated on host arch"
    assert "#1261" in text, "the skip must be traceable to its tracking issue"


def test_bring_up_converges_runtime_roles_after_migrations() -> None:
    """The live-stack path never runs the compose app tier (its backend set excludes the
    role-bootstrap one-shot), so stack-services.sh itself must converge the runtime login
    members — after
    migrations create the NOLOGIN capabilities and before the host processes (and the installed
    worker fleet they activate) authenticate (#2036)."""
    text = (ROOT / "scripts/live-stack/stack-services.sh").read_text()
    assert (
        text.index('banner "migrations')
        < text.index("docker compose run --rm --no-deps role-bootstrap")
        < text.index('banner "host processes"')
    ), "role-bootstrap must run after migrations and before host processes"
    assert "runtime-role bootstrap failed" in text, (
        "a failed convergence must fail bring-up loudly, not leave members silently missing"
    )


def test_role_bootstrap_keeps_the_external_provisioning_escape_hatch() -> None:
    """KDIVE_LOCAL_ROLE_BOOTSTRAP=0 (externally provisioned, retained hosts) must keep its
    no-database-mutation contract: stack-services.sh skips the one-shot entirely instead of
    running it and relying on the script's own =0 no-op (#2036)."""
    text = (ROOT / "scripts/live-stack/stack-services.sh").read_text()
    assert '[[ "${KDIVE_LOCAL_ROLE_BOOTSTRAP:-1}" == "1" ]]' in text, (
        "bring-up must honor the external-provisioning escape hatch"
    )


def test_role_bootstrap_runs_with_the_container_internal_migration_dsn() -> None:
    """env.sh exports the host-facing migration DSN (localhost), which is unreachable from
    inside the compose network; stack-services.sh must unset it for the one-shot so the compose
    default (postgres:5432) applies, without duplicating the development credential literal."""
    text = (ROOT / "scripts/live-stack/stack-services.sh").read_text()
    assert "env -u KDIVE_MIGRATION_DATABASE_URL" in text
    assert "postgresql://kdive-migration" not in text, (
        "stack-services.sh must not re-declare the migration DSN literal; the compose default "
        "owns it"
    )


def _ensure_session_libvirtd(
    tmp_path: Path, daemon: str = "libvirtd", *, positional: bool = True
) -> subprocess.CompletedProcess[str]:
    """Source the real lib.sh and run ensure_session_libvirtd against staged paths.

    `positional=False` drops the three overrides so the call exercises the defaults, which are
    derived from the published URI's daemon family rather than hardcoded.
    """
    for name in ("lib.sh", "libvirt-uri.sh"):
        (tmp_path / name).write_text(
            (ROOT / "scripts/live-stack" / name).read_text(), encoding="utf-8"
        )
    args = ""
    if positional:
        args = (
            f'"{tmp_path / f"{daemon}-stub"}" "{tmp_path / f"{daemon}-live.conf"}" '
            f'"{tmp_path / "run/kdive/live-libvirt"}"'
        )
    socket_name = "virtqemud-sock" if daemon == "virtqemud" else "libvirt-sock"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "$1"; ensure_session_libvirtd {args}',
            "bash",
            str(tmp_path / "lib.sh"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_libvirt_env(
            LIBVIRT_ENV=str(tmp_path / "no-contract.env"),
            KDIVE_LIBVIRT_URI=(
                f"qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/{socket_name}"
            ),
        ),
    )
    # `bash -c` carries no `set -e`, so a lib.sh that half-sourced would be invisible: every
    # assertion below is about ensure_session_libvirtd's own behavior and would still pass. The
    # staged copy must bring every sibling lib.sh sources.
    assert "No such file or directory" not in result.stderr, result.stderr
    return result


@contextmanager
def _session_daemon_stage(tmp_path: Path, daemon: str = "libvirtd") -> Generator[Path]:
    """Stage conf + runtime root and a daemon stub recording argv and XDG_RUNTIME_DIR."""
    (tmp_path / f"{daemon}-live.conf").write_text("# staged config\n", encoding="utf-8")
    (tmp_path / "run" / "kdive" / "live-libvirt").mkdir(parents=True)
    calls = tmp_path / f"{daemon}.calls"
    stub = tmp_path / f"{daemon}-stub"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >>"$(dirname "$0")/{daemon}.calls"\n'
        f'env | grep \'^XDG_RUNTIME_DIR=\' >>"$(dirname "$0")/{daemon}.calls"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    yield calls


@pytest.mark.parametrize("daemon", ("libvirtd", "virtqemud"))
def test_ensure_session_libvirtd_follows_the_published_daemon_family(
    tmp_path: Path, daemon: str
) -> None:
    """#2480: every entry point now resolves the published URI, so this recovery branch is
    reachable on Red Hat- and SUSE-family runners for the first time. The lifecycle installer
    selects virtqemud there and installs only /etc/kdive/virtqemud-live.conf, so the former
    hardcoded libvirtd defaults named a binary, a config and a pid file that host has never had.
    The published URI's socket basename is what carries the family."""
    bare = _ensure_session_libvirtd(tmp_path, daemon, positional=False)
    assert bare.returncode == 1
    assert f"daemon binary: /usr/sbin/{daemon} " in bare.stderr
    assert f"config:        /etc/kdive/{daemon}-live.conf " in bare.stderr

    # The pid file is never a positional override, so the success path is the only place its
    # derived name is observable.
    with _session_daemon_stage(tmp_path, daemon) as calls:
        result = _ensure_session_libvirtd(tmp_path, daemon)
        assert result.returncode == 0, result.stderr
        expected_pid = tmp_path / f"run/kdive/live-libvirt/libvirt/{daemon}.pid"
        assert f"--pid-file {expected_pid}" in calls.read_text(encoding="utf-8")


def test_ensure_session_libvirtd_starts_the_operator_owned_daemon(tmp_path: Path) -> None:
    """No pid: start exactly what provisioning starts, as the invoking user (#2032)."""
    with _session_daemon_stage(tmp_path) as calls:
        result = _ensure_session_libvirtd(tmp_path)
        assert result.returncode == 0, result.stderr
        recorded = calls.read_text(encoding="utf-8").splitlines()
        expected_pid = str(tmp_path / "run/kdive/live-libvirt/libvirt/libvirtd.pid")
        assert recorded == [
            f"--daemon --config {tmp_path / 'libvirtd-live.conf'} --pid-file {expected_pid}",
            f"XDG_RUNTIME_DIR={tmp_path / 'run/kdive/live-libvirt'}",
        ]


def test_ensure_session_libvirtd_is_idempotent_on_a_live_pid(tmp_path: Path) -> None:
    """A pid file pointing at a live process short-circuits without spawning anything."""
    with _session_daemon_stage(tmp_path) as calls:
        pid_dir = tmp_path / "run/kdive/live-libvirt/libvirt"
        pid_dir.mkdir()
        (pid_dir / "libvirtd.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        result = _ensure_session_libvirtd(tmp_path)
        assert result.returncode == 0, result.stderr
        assert not calls.exists(), "a live daemon must not be started a second time"


def test_ensure_session_libvirtd_recovers_from_a_stale_pid_file(tmp_path: Path) -> None:
    """A dead pid is not a daemon: the fallback must still bring the endpoint up."""
    with _session_daemon_stage(tmp_path) as calls:
        pid_dir = tmp_path / "run/kdive/live-libvirt/libvirt"
        pid_dir.mkdir()
        (pid_dir / "libvirtd.pid").write_text("999999999\n", encoding="utf-8")
        result = _ensure_session_libvirtd(tmp_path)
        assert result.returncode == 0, result.stderr
        assert calls.exists()


def test_ensure_session_libvirtd_fails_loud_naming_the_exact_paths(tmp_path: Path) -> None:
    """Missing prerequisites die with every path named — no silent degradation (#2032)."""
    (tmp_path / "run" / "kdive" / "live-libvirt").mkdir(parents=True)
    result = _ensure_session_libvirtd(tmp_path)
    assert result.returncode != 0
    for path in (
        tmp_path / "libvirtd-stub",
        tmp_path / "libvirtd-live.conf",
    ):
        assert str(path) in result.stderr
    assert "sudo" not in result.stderr


def test_up_session_recovery_path_starts_the_daemon_without_sudo() -> None:
    """#2032: starting the operator-owned session daemon stays unprivileged -- the runner
    service account has no sudo. #2503 adds a separate, best-effort virtnodedevd remediation
    later in this same branch that may invoke sudo; this test scopes to daemon startup only."""
    text = (ROOT / "scripts/live-stack/stack-services.sh").read_text()
    gate = text.index('*"live-libvirt"*')
    start_call = text.index("ensure_session_libvirtd", gate)
    end_call = text.index("\n      }\n", start_call)
    daemon_start = text[gate:end_call]
    executed = [line for line in daemon_start.splitlines() if not line.lstrip().startswith("#")]
    assert "sudo" not in "\n".join(executed), "starting the session daemon must stay unprivileged"
    assert "sudo systemctl enable --now virtqemud.socket" in text


def test_up_bare_host_branch_enables_virtnodedevd_alongside_virtqemud() -> None:
    """#2401: onboarding's resource discovery needs virtnodedevd, not just virtqemud."""
    text = (ROOT / "scripts/live-stack/stack-services.sh").read_text()
    bare_host_branch = text.index("# Bare dev host")
    libvirt_ok_gate = text.index("libvirt_ok || {", bare_host_branch)
    branch = text[bare_host_branch:libvirt_ok_gate]
    assert "sudo systemctl enable --now virtqemud.socket virtnodedevd.socket" in branch


def _session_recovery_snippet() -> str:
    """The exact session-daemon recovery `if ... fi` block, standalone-executable."""
    text = (ROOT / "scripts/live-stack/stack-services.sh").read_text()
    start = text.index('if [[ "$KDIVE_LIBVIRT_URI" == *"live-libvirt"* ]]; then')
    end = text.index("\n    fi\n", start) + len("\n    fi")
    return text[start:end]


def _run_session_recovery_snippet(tmp_path: Path, *, libvirt_uri: str) -> list[str]:
    """Execute the session-daemon recovery branch standalone: stub `ensure_session_libvirtd` to
    succeed and record every `sudo` invocation, then return the recorded argv lines."""
    calls = tmp_path / "sudo.calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo_stub = bin_dir / "sudo"
    sudo_stub.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >>'{calls}'\n", encoding="utf-8"
    )
    sudo_stub.chmod(0o755)
    script = f"ensure_session_libvirtd() {{ return 0; }}\n{_session_recovery_snippet()}\n"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["KDIVE_LIBVIRT_URI"] = libvirt_uri
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, env=env
    )
    assert result.returncode == 0, result.stderr
    return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def test_session_recovery_enables_virtnodedevd_for_the_modular_family(tmp_path: Path) -> None:
    """#2503: the modular virtqemud session daemon (Red Hat/SUSE family) proxies node-device
    queries to the system virtnodedevd socket, which the session recovery branch never enabled --
    docs/design/2026-09-09-ppc64le-emulated-power-live-proof-2383-proof-record.md:284-286 records
    the resulting connection failure. Mirror the bare-host branch's remediation here too."""
    calls = _run_session_recovery_snippet(
        tmp_path,
        libvirt_uri="qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock",
    )
    assert any("systemctl enable --now virtnodedevd.socket" in call for call in calls), calls


def test_session_recovery_leaves_the_monolithic_family_alone(tmp_path: Path) -> None:
    """The Debian-family session daemon is the monolithic libvirtd, which answers node-device
    queries itself (lib.sh's nodedev_ok comment) -- it needs no virtnodedevd unit (#2503)."""
    calls = _run_session_recovery_snippet(
        tmp_path,
        libvirt_uri="qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock",
    )
    assert not any("virtnodedevd" in call for call in calls), calls


def test_up_checks_virtnodedevd_reachability_and_names_the_unit_on_failure() -> None:
    """#2401 acceptance: a missing daemon fails in stack-services.sh naming the unit, not in
    discovery.py."""
    text = (ROOT / "scripts/live-stack/stack-services.sh").read_text()
    gate = text.index("libvirt_ok || {")
    check = text.index("nodedev_ok || {", gate)
    block = text[check : text.index("}", check)]
    assert "virtnodedevd" in block


def _up_remediation_gate_condition() -> str:
    """The exact `if ...; then` line stack-services.sh uses to decide whether to remediate
    libvirt."""
    text = (ROOT / "scripts/live-stack/stack-services.sh").read_text()
    start = text.index("if ! libvirt_ok")
    return text[start : text.index("\n", start)]


def _remediation_gate_fires(tmp_path: Path, *, list_ok: bool, nodedev_list_ok: bool) -> bool:
    """Source the real lib.sh (so libvirt_ok/nodedev_ok are stack-services.sh's own functions)
    with a stubbed `virsh`, then evaluate stack-services.sh's own extracted gate condition line.
    True means
    stack-services.sh would enter its remediation branch for this virsh behavior (#2401)."""
    lib_sh_copy = tmp_path / "lib.sh"
    for name in ("lib.sh", "libvirt-uri.sh"):
        (tmp_path / name).write_text(
            (ROOT / "scripts/live-stack" / name).read_text(), encoding="utf-8"
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "virsh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        # Invoked as `virsh -c "$KDIVE_LIBVIRT_URI" list|nodedev-list`; the subcommand is
        # the last argument, so match on it rather than a fixed position.
        'case "${*: -1}" in\n'
        f"  list) exit {0 if list_ok else 1} ;;\n"
        f"  nodedev-list) exit {0 if nodedev_list_ok else 1} ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    condition = _up_remediation_gate_condition()
    script = (
        'source "$1"\n'
        'KDIVE_LIBVIRT_URI="qemu:///system"\n'
        f"{condition} echo ENTERS_REMEDIATION; else echo SKIPS_REMEDIATION; fi\n"
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(lib_sh_copy)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "REMEDIATION" in result.stdout, result.stdout
    return "ENTERS_REMEDIATION" in result.stdout


def test_up_remediation_gate_fires_when_only_virtnodedevd_is_unreachable(tmp_path: Path) -> None:
    """#2401: virtqemud already healthy (`virsh list` ok) but virtnodedevd never enabled
    (`virsh nodedev-list` fails) must still enter the remediation branch -- the exact host
    state issue #2401 names as its trigger, not just a fully-down libvirt."""
    assert _remediation_gate_fires(tmp_path, list_ok=True, nodedev_list_ok=False)


def test_up_remediation_gate_skips_when_both_daemons_are_already_reachable(tmp_path: Path) -> None:
    """No regression: a fully healthy host must not re-run the remediation branch."""
    assert not _remediation_gate_fires(tmp_path, list_ok=True, nodedev_list_ok=True)


def test_lifecycle_wrapper_uses_the_validated_public_uri_and_python_client() -> None:
    text = (ROOT / "scripts/live-stack/worker-lifecycle.sh").read_text()
    parser = LIBVIRT_URI.read_text()
    assert "live-worker-libvirt.env" in parser
    assert 'source "$LIBVIRT_ENV"' not in text
    assert 'source "${here}/libvirt-uri.sh"' in text
    assert "LIBVIRT_SOCKET_URIS" in parser
    assert "LifecycleRequest.model_validate" in text
    assert "request_path" in text
    assert "KDIVE_WORKER_DATABASE_URL" in text


def test_lifecycle_launcher_covers_required_worker_settings_and_authority_geometry() -> None:
    from kdive.processes.lifecycle.systemd.systemd_worker_contract import WorkerSettings

    program = LIFECYCLE.read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    dictionaries = [node for node in ast.walk(ast.parse(program)) if isinstance(node, ast.Dict)]
    settings_keys = next(
        keys
        for node in dictionaries
        if "worker_database_url"
        in (keys := {key.value for key in node.keys if isinstance(key, ast.Constant)})
    )
    required = {name for name, field in WorkerSettings.model_fields.items() if field.is_required()}
    authority = {
        name
        for name in WorkerSettings.model_fields
        if name.startswith("authority_") or name == "external_boot_capacity_bytes"
    }
    assert required | authority <= settings_keys
    assert settings_keys <= WorkerSettings.model_fields.keys()
    assert "libvirt_recovery_root" not in settings_keys


def test_lifecycle_start_rejects_mismatched_authority_geometry_before_request() -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"\n'
            'uri="$2"\n'
            "require_compatible_lifecycle() { :; }\n"
            "require_start_prerequisites() { :; }\n"
            'load_published_libvirt_uri() { printf %s "$uri"; }\n'
            "request start 1",
            "bash",
            str(LIFECYCLE),
            "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "KDIVE_ROOTFS_DIR": "/tmp/rootfs",
            "KDIVE_BUILD_WORKSPACE": "/tmp/build",
            "KDIVE_BUILD_COMPONENT_ROOTS": "/tmp/fixtures",
            "KDIVE_INSTALL_STAGING": "/tmp/install",
            "KDIVE_FIXTURE_CATALOG_PATH": "/tmp/fixtures",
            "KDIVE_KERNEL_SRC": "/tmp/kernel",
            "KDIVE_WORKER_DATABASE_URL": "postgresql://worker-member/kdive",
            "AWS_ACCESS_KEY_ID": "access-key",
            "AWS_SECRET_ACCESS_KEY": "secret-key",  # pragma: allowlist secret
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "authority-a",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET": "/run/authority.sock",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF": "authority/server-ca",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF": "authority/client-cert",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF": (
                "authority/client-key"  # pragma: allowlist secret
            ),
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_STORE_IDENTITY": "authority-store",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_RESERVE_BYTES": "4096",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_MAX_BYTES": "8192",
            "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES": "4097",
        },
    )

    assert result.returncode == 2
    assert result.stderr == "lifecycle request construction failed safely\n"


@pytest.mark.parametrize(
    ("content", "success"),
    [
        (
            "KDIVE_LIBVIRT_URI=qemu+unix:///session?socket="
            "/run/kdive/live-libvirt/libvirt/libvirt-sock\n",
            True,
        ),
        ("KDIVE_LIBVIRT_URI=$(touch TMP_CANARY)\n", False),
        ("KDIVE_LIBVIRT_URI=not-a-supported-uri\n", False),
    ],
)
def test_lifecycle_uri_is_parsed_as_literal_data(
    tmp_path: Path, content: str, success: bool
) -> None:
    wrapper = tmp_path / "worker-lifecycle.sh"
    (tmp_path / "lib.sh").write_text(
        (ROOT / "scripts/live-stack/lib.sh").read_text(), encoding="utf-8"
    )
    (tmp_path / "env.sh").write_text(
        (ROOT / "scripts/live-stack/env.sh").read_text(), encoding="utf-8"
    )
    wrapper.write_text(LIFECYCLE.read_text(), encoding="utf-8")
    (tmp_path / "libvirt-uri.sh").write_text(LIBVIRT_URI.read_text(), encoding="utf-8")
    uri_file = tmp_path / "libvirt.env"
    canary = tmp_path / "unsafe"
    uri_file.write_text(content.replace("TMP_CANARY", str(canary)), encoding="utf-8")
    uri_file.chmod(0o644)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && require_exact_libvirt_env() { :; } && load_published_libvirt_uri',
            "bash",
            str(wrapper),
        ],
        capture_output=True,
        text=True,
        check=False,
        # LIBVIRT_ENV points the parser at the staged file (it is overridable since #2480, which
        # replaced the string surgery this test used to do). A preset KDIVE_LIBVIRT_URI keeps
        # source-time resolution off the staged file, whose test-owned metadata it would reject;
        # the explicit `load_published_libvirt_uri` call below is what this test is about.
        env={**os.environ, "LIBVIRT_ENV": str(uri_file), "KDIVE_LIBVIRT_URI": "qemu:///system"},
    )
    assert (result.returncode == 0) is success, result.stderr
    assert not canary.exists()


def test_host_migrations_default_to_the_compose_migration_owner() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"KDIVE_DATABASE_URL", "KDIVE_MIGRATION_DATABASE_URL"}
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; printf "%s\\n" "$KDIVE_MIGRATION_DATABASE_URL"',
            "bash",
            str(ROOT / "scripts/live-stack/env.sh"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    expected_login = "kdive-migration:kdive-migration-local"  # pragma: allowlist secret
    assert result.stdout == f"postgresql://{expected_login}@localhost:5432/kdive\n"


def test_role_database_dsns_are_never_env_program_arguments() -> None:
    for relative_path in (
        "scripts/live-stack/apply-migrations.sh",
        "scripts/live-stack/lib.sh",
        "scripts/live-stack/stack-status.sh",
        "scripts/live-stack/stack-services.sh",
    ):
        logical_lines = (ROOT / relative_path).read_text(encoding="utf-8").replace("\\\n", " ")
        for line in logical_lines.splitlines():
            if re.search(r"\benv\b.*DATABASE_URL=", line):
                pytest.fail(f"database DSN exposed in env argv: {relative_path}: {line.strip()}")


def test_lifecycle_status_preserves_non_ok_response_and_scrubs_other_role_dsns(
    tmp_path: Path,
) -> None:
    response = _response(ok=False, code="busy", slots=[_slot(1, "terminated")])
    result = _lifecycle_status(tmp_path, response, expected_slots="2")
    assert result.returncode == 3
    assert '"code":"busy"' in result.stdout
    assert "lifecycle status does not report" not in result.stderr
    assert (tmp_path / "environment").read_text(encoding="utf-8").splitlines() == [
        "<missing>",
        "<missing>",
        "<missing>",
        "<missing>",
        "worker-canary",
    ]


def test_apply_migrations_runs_with_runtime_role_dsns_scrubbed(tmp_path: Path) -> None:
    """The host migrator must connect through the migration authority alone (#1929).

    A stub `uv` records which KDIVE_*DATABASE_URL variables survive into the migration process:
    only the migration owner's may. Any runtime role DSN present would let a migration-side
    regression silently run against (or leak) a server/worker/reconciler authority.
    """
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nenv | grep "^KDIVE_.*DATABASE_URL=" | sort > "$KDIVE_MIGRATE_PROBE"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    probe = tmp_path / "environment"
    pg_port = _free_port()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KDIVE_") or not key.endswith("DATABASE_URL")
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/live-stack/apply-migrations.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={
            **environment,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "KDIVE_MIGRATE_PROBE": str(probe),
            "KDIVE_POSTGRES_PORT": str(pg_port),
            "KDIVE_SERVER_DATABASE_URL": "server-canary",
            "KDIVE_WORKER_DATABASE_URL": "worker-canary",
            "KDIVE_RECONCILER_DATABASE_URL": "reconciler-canary",
        },
    )
    assert result.returncode == 0, result.stderr
    expected_login = "kdive-migration:kdive-migration-local"  # pragma: allowlist secret
    assert probe.read_text(encoding="utf-8").splitlines() == [
        f"KDIVE_MIGRATION_DATABASE_URL=postgresql://{expected_login}@localhost:{pg_port}/kdive"
    ]


def test_status_database_probe_scrubs_unrelated_role_dsns(tmp_path: Path) -> None:
    status = tmp_path / "stack-status.sh"
    source = (ROOT / "scripts/live-stack/stack-status.sh").read_text()
    setup = source[: source.index('echo "=== compose')]
    # The libvirt section opens with its unresolved-endpoint guard, not with its banner: since
    # ADR-0659 the banner is inside that `if`, so slicing to the banner would cut the block in
    # half and leave the extract syntactically unclosed.
    database = source[
        source.index('echo "=== database') : source.index('if [[ -n "${LIBVIRT_UNRESOLVED}" ]]')
    ]
    status.write_text(setup + database + "exit 0\n", encoding="utf-8")
    for name in ("lib.sh", "env.sh", "libvirt-uri.sh"):
        (tmp_path / name).write_text(
            (ROOT / "scripts/live-stack" / name).read_text(), encoding="utf-8"
        )
    worker = tmp_path / "worker-lifecycle.sh"
    worker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    worker.chmod(0o755)
    probe = tmp_path / "environment"
    python = tmp_path / "python"
    python.write_text(
        "#!/bin/sh\nenv | grep '^KDIVE_.*DATABASE_URL=' > \"$KDIVE_STATUS_PROBE\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    result = subprocess.run(
        ["bash", str(status)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "KDIVE_PYTHON": str(python),
            "KDIVE_STATUS_PROBE": str(probe),
            "KDIVE_SERVER_DATABASE_URL": "server-canary",
            "KDIVE_MIGRATION_DATABASE_URL": "migration-canary",
            "KDIVE_WORKER_DATABASE_URL": "worker-canary",
            "KDIVE_RECONCILER_DATABASE_URL": "reconciler-canary",
        },
    )
    assert result.returncode == 0, result.stderr
    assert set(probe.read_text(encoding="utf-8").splitlines()) == {
        "KDIVE_DATABASE_URL=server-canary",
        "KDIVE_SERVER_DATABASE_URL=server-canary",
    }


def test_onboard_aliases_each_cli_invocation_to_its_own_authority(tmp_path: Path) -> None:
    """onboard.sh must hand each kdive CLI child only its own database authority (#2046).

    env.sh stopped exporting a shared KDIVE_DATABASE_URL (#1929), so a bare `-m kdive ...`
    invocation dies at config validation (the setting is required-always for runnable
    processes). A stub interpreter records the argv and the five DSN variables it was handed:
    migrate must see the migration authority (schema-current migrate still SELECTs
    schema_migrations, granted to no runtime role), seed/verify the server authority, and the
    OIDC-only mint step no database authority at all.
    """
    onboard_dir = tmp_path / "scripts/live-stack"
    onboard_dir.mkdir(parents=True)
    for name in ("env.sh", "libvirt-uri.sh"):
        (onboard_dir / name).write_text(
            (ROOT / "scripts/live-stack" / name).read_text(), encoding="utf-8"
        )
    onboard = onboard_dir / "onboard.sh"
    source = (ROOT / "scripts/live-stack/onboard.sh").read_text()
    # The mint heredoc imports kdive; the stub never sees it, so end the script after the last
    # database-touching invocation — the aliasing contract under test is fully recorded by then.
    onboard.write_text(source[: source.index('banner "token + contract"')], encoding="utf-8")
    probe = tmp_path / "environment"
    python = tmp_path / "python"
    python.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s|%s|%s|%s|%s\n' \"$*\" "
        '"${KDIVE_DATABASE_URL:-<missing>}" '
        '"${KDIVE_MIGRATION_DATABASE_URL:-<missing>}" '
        '"${KDIVE_SERVER_DATABASE_URL:-<missing>}" '
        '"${KDIVE_WORKER_DATABASE_URL:-<missing>}" '
        '"${KDIVE_RECONCILER_DATABASE_URL:-<missing>}" >> "$KDIVE_ONBOARD_PROBE"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KDIVE_") or not key.endswith("DATABASE_URL")
    }
    result = subprocess.run(
        ["bash", str(onboard)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **environment,
            "KDIVE_PYTHON": str(python),
            "KDIVE_ONBOARD_PROBE": str(probe),
            "KDIVE_MIGRATION_DATABASE_URL": "migration-canary",
            "KDIVE_SERVER_DATABASE_URL": "server-canary",
            "KDIVE_WORKER_DATABASE_URL": "worker-canary",
            "KDIVE_RECONCILER_DATABASE_URL": "reconciler-canary",
        },
    )
    assert result.returncode == 0, result.stderr
    missing = "<missing>"
    observed = {}
    for row in probe.read_text(encoding="utf-8").splitlines():
        argv, dsns = row.split("|", 1)
        observed[argv.split(" --", 1)[0]] = dsns.split("|")
    assert observed == {
        "-m kdive migrate": [
            "migration-canary",
            "migration-canary",
            missing,
            missing,
            missing,
        ],
        "-m kdive seed-project": [
            "server-canary",
            missing,
            "server-canary",
            missing,
            missing,
        ],
        "-m kdive verify-project": [
            "server-canary",
            missing,
            "server-canary",
            missing,
            missing,
        ],
    }


def test_host_daemon_children_receive_only_their_role_database_authority(tmp_path: Path) -> None:
    lifecycle = tmp_path / "scripts/live-stack"
    lifecycle.mkdir(parents=True)
    (lifecycle / "worker-lifecycle.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (lifecycle / "worker-lifecycle.sh").chmod(0o755)
    probe = tmp_path / "environment"
    python = tmp_path / "python"
    python.write_text(
        "#!/bin/sh\nprintf '%s|%s|%s|%s|%s|%s\\n' \"$*\" "
        '"${KDIVE_DATABASE_URL:-<missing>}" '
        '"${KDIVE_MIGRATION_DATABASE_URL:-<missing>}" '
        '"${KDIVE_SERVER_DATABASE_URL:-<missing>}" '
        '"${KDIVE_WORKER_DATABASE_URL:-<missing>}" '
        '"${KDIVE_RECONCILER_DATABASE_URL:-<missing>}" >> "$KDIVE_DAEMON_PROBE"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    result = _lib(
        f'repo_root="{tmp_path}"\n'
        f'py="{python}"\n'
        f'log_dir="{tmp_path / "logs"}"\n'
        "stop_daemons() { :; }\n"
        "require_free_http_port() { :; }\n"
        "wait_for_daemons_to_settle() { :; }\n"
        "restart_host_processes\n",
        KDIVE_WORKER_COUNT="1",
        KDIVE_DAEMON_PROBE=str(probe),
        KDIVE_MIGRATION_DATABASE_URL="migration-canary",
        KDIVE_SERVER_DATABASE_URL="server-canary",
        KDIVE_WORKER_DATABASE_URL="worker-canary",
        KDIVE_RECONCILER_DATABASE_URL="reconciler-canary",
    )
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (
        not probe.exists() or len(probe.read_text(encoding="utf-8").splitlines()) < 2
    ):
        time.sleep(0.05)
    rows = probe.read_text(encoding="utf-8").splitlines()
    assert set(rows) == {
        "-m kdive server|server-canary|<missing>|server-canary|<missing>|<missing>",
        "-m kdive reconciler|reconciler-canary|<missing>|<missing>|<missing>|reconciler-canary",
    }


def test_restart_host_processes_routes_the_fleet_through_the_lifecycle_boundary(
    tmp_path: Path,
) -> None:
    """Bring-up must drive workers only through the witness, in its request order (#1938).

    A recording stub stands in for the installed `worker-lifecycle.sh` (found via the overridden
    repo_root), and a stub interpreter proves no `-m kdive worker` child is spawned directly. The
    recorded sequence is the contract: sweep diagnostics, retire any retained fleet, start exactly
    the configured count, then gate on status reporting those slots as started.
    """
    events = tmp_path / "lifecycle-events"
    script_dir = tmp_path / "scripts" / "live-stack"
    script_dir.mkdir(parents=True)
    lifecycle = script_dir / "worker-lifecycle.sh"
    lifecycle.write_text(
        "#!/bin/sh\n"
        f'printf "%s expected=%s\\n" "$*" "${{KDIVE_LIFECYCLE_EXPECTED_SLOTS:-<unset>}}"'
        f' >>"{events}"\n',
        encoding="utf-8",
    )
    lifecycle.chmod(0o755)
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    result = _lib(
        f'repo_root="{tmp_path}"\n'
        f'py="{python}"\n'
        f'log_dir="{tmp_path / "logs"}"\n'
        "stop_daemons() { :; }\n"
        "require_free_http_port() { :; }\n"
        "wait_for_daemons_to_settle() { :; }\n"
        "restart_host_processes || echo 'BRING_UP_FAILED'\n"
        'echo "DAEMON_COUNT=${DAEMON_COUNT}"\n',
        KDIVE_WORKER_COUNT="3",
    )
    assert result.returncode == 0, result.stderr
    assert "BRING_UP_FAILED" not in result.stdout, (
        f"bring-up must succeed against the stubs: {result.stdout}\n{result.stderr}"
    )
    assert "DAEMON_COUNT=2" in result.stdout, (
        f"the settle gate must expect only server + reconciler: {result.stdout}"
    )
    assert events.read_text().splitlines() == [
        "diagnostics expected=<unset>",
        "stop expected=<unset>",
        "start 3 expected=<unset>",
        "status expected=3",
    ], f"the witness request sequence changed: {events.read_text()}"
    # Workers are witness-owned units, so bring-up must not open checkout-side worker logs.
    daemon_logs = sorted((tmp_path / "logs").glob("*.log"))
    assert [p.name for p in daemon_logs] == ["reconciler.log", "server.log"], (
        f"workers must not get checkout-side log files: {daemon_logs}"
    )


@pytest.mark.parametrize(
    "slots",
    [
        [],
        [_slot(1)],
        [_slot(1), _slot(2), _slot(3)],
        [_slot(1), _slot(2, "terminated")],
    ],
)
def test_lifecycle_status_rejects_any_non_exact_successful_slot_set(
    tmp_path: Path, slots: list[dict[str, object]]
) -> None:
    response = _response(ok=True, code="ok", slots=slots)
    result = _lifecycle_status(tmp_path, response, expected_slots="2")
    assert result.returncode == 5
    assert "lifecycle status does not report the requested started slots" in result.stderr
    assert '"ok":true' not in result.stdout


def test_lifecycle_status_accepts_exact_successful_started_slots(tmp_path: Path) -> None:
    result = _lifecycle_status(
        tmp_path,
        _response(ok=True, code="ok", slots=[_slot(1), _slot(2)]),
        expected_slots="2",
    )
    assert result.returncode == 0, result.stderr


def test_lifecycle_status_public_syntax_does_not_accept_a_count() -> None:
    result = subprocess.run(
        ["bash", str(LIFECYCLE), "status", "2"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_lifecycle_request_construction_hides_oversized_secret_canaries(tmp_path: Path) -> None:
    canary = "CREDENTIAL_PREFIX_" + "x" * 5000 + "_CREDENTIAL_SUFFIX"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"\n'
            'uri="$2"\n'
            "require_compatible_lifecycle() { :; }\n"
            "require_start_prerequisites() { :; }\n"
            'load_published_libvirt_uri() { printf %s "$uri"; }\n'
            "request start 1",
            "bash",
            str(LIFECYCLE),
            "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "KDIVE_ROOTFS_DIR": "/tmp/rootfs",
            "KDIVE_BUILD_WORKSPACE": "/tmp/build",
            "KDIVE_BUILD_COMPONENT_ROOTS": "/tmp/fixtures",
            "KDIVE_INSTALL_STAGING": "/tmp/install",
            "KDIVE_FIXTURE_CATALOG_PATH": "/tmp/fixtures",
            "KDIVE_KERNEL_SRC": "/tmp/kernel",
            "KDIVE_WORKER_DATABASE_URL": canary,
            "AWS_ACCESS_KEY_ID": canary,
            "AWS_SECRET_ACCESS_KEY": canary,
        },
    )
    assert result.returncode == 2
    assert "lifecycle request construction failed safely" in result.stderr
    assert "CREDENTIAL_PREFIX_" not in result.stdout + result.stderr
    assert "_CREDENTIAL_SUFFIX" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("mode", "groups", "file_group", "expected"),
    [("50", " 9 ", "9", "5"), ("70", " 9 ", "9", "7"), ("7", " 8 ", "9", "7")],
)
def test_permission_bits_pad_short_gnu_stat_octal(
    mode: str, groups: str, file_group: str, expected: str
) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && account_permission_bits 1 "$2" 2 "$3" "$4"',
            "bash",
            str(LIFECYCLE),
            groups,
            file_group,
            mode,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def _worker_path_access(
    tmp_path: Path, target: Path, permissions: str, groups: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"\n'
            'worker_groups="$3"\n'
            'id() { if [[ $1 == -u ]]; then echo 424242; else echo "$worker_groups"; fi; }\n'
            'require_worker_path_access "$2" "$4" "test path"',
            "bash",
            str(LIFECYCLE),
            str(target),
            groups,
            permissions,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_lifecycle_preflight_rejects_a_worker_denied_by_a_parent(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    target = parent / "kernel"
    target.mkdir(mode=0o755)
    result = _worker_path_access(tmp_path, target, "rx", "424242")
    assert result.returncode != 0
    assert "test path is not accessible to kdive-worker-1" in result.stderr


@pytest.mark.parametrize(("mode", "success"), [(0o2750, False), (0o2770, True)])
def test_lifecycle_preflight_interprets_setgid_group_permissions(
    tmp_path: Path, mode: int, success: bool
) -> None:
    workspace = Path("/tmp") / f"kdive-workspace-{os.getpid()}"
    workspace.mkdir(mode=mode)
    workspace.chmod(mode)
    try:
        result = _worker_path_access(tmp_path, workspace, "rwx", str(os.getgid()))
    finally:
        workspace.rmdir()
    assert (result.returncode == 0) is success, result.stderr


def test_lifecycle_preflight_resolves_a_symlink_before_checking_ancestry(tmp_path: Path) -> None:
    denied_parent = tmp_path / "private"
    denied_parent.mkdir(mode=0o700)
    target = denied_parent / "kernel"
    target.mkdir(mode=0o755)
    link = tmp_path / "kernel-link"
    link.symlink_to(target, target_is_directory=True)
    result = _worker_path_access(tmp_path, link, "rx", "424242")
    assert result.returncode != 0


def test_down_blocks_backend_teardown_after_an_unresolved_lifecycle_stop() -> None:
    text = (ROOT / "scripts/live-stack/stack-down.sh").read_text()
    assert text.index('worker-lifecycle.sh" stop') < text.index("stopping compose backends")
    assert "unresolved evidence; backends remain up" in text
    assert "may strand fences" in text


_JUSTFILE = ROOT / "justfile"
_JUST = shutil.which("just")


def _stub_docker_recording_invocations(bin_dir: Path, log: Path) -> None:
    """Write a stub `docker` that appends every invocation to `log` and reports the local
    `kdive-mock-oidc:dev` image as absent, so `stack-backends`'s local-build branch (when taken)
    always reaches `docker compose build oidc` rather than the "already cached" no-op."""
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "{log}"\n'
        '[[ "$1 $2 $3" == "image inspect kdive-mock-oidc:dev" ]] && exit 1\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _stub_live_stack_scripts(root: Path, *, oidc_image: str | None) -> Path:
    """Populate a fake checkout under `root` so the bring-up script's `backends` stage can run
    without a live stack, and return the staged `stack-services.sh`.

    The bring-up script and `lib.sh` are the REAL files — the stage gate and the readiness
    contract are what these tests exercise. Everything the stage reaches outside them is a
    stand-in: `env.sh`, the ONLY place `KDIVE_OIDC_IMAGE` is set (ADR-0358), exporting
    `oidc_image` when given (simulating ppc64le-under-qemu auto-detection) or nothing (the
    undetected/x86_64 case); a no-op `apply-migrations.sh` so the tail needs no database; and a
    `.venv` interpreter so the preflight's `-x` check passes.
    """
    live_stack = root / "scripts" / "live-stack"
    live_stack.mkdir(parents=True)
    for name in ("stack-services.sh", "lib.sh", "libvirt-uri.sh"):
        shutil.copy(ROOT / "scripts" / "live-stack" / name, live_stack / name)
        (live_stack / name).chmod(0o755)
    export_line = f'export KDIVE_OIDC_IMAGE="{oidc_image}"\n' if oidc_image is not None else ""
    env_sh = live_stack / "env.sh"
    env_sh.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{export_line}", encoding="utf-8")
    env_sh.chmod(0o755)
    apply_migrations = live_stack / "apply-migrations.sh"
    apply_migrations.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    apply_migrations.chmod(0o755)
    venv_python = root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    venv_python.chmod(0o755)
    return live_stack / "stack-services.sh"


def _run_stack_up(
    tmp_path: Path, *, oidc_image: str | None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Drive the real `stack-backends` recipe with a stubbed `docker` and a fake live-stack
    checkout, and return its result plus the log of every `docker` invocation it made."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    log.touch()
    _stub_docker_recording_invocations(bin_dir, log)
    _stub_live_stack_scripts(tmp_path, oidc_image=oidc_image)
    assert _JUST is not None
    result = subprocess.run(
        [
            _JUST,
            "--justfile",
            str(_JUSTFILE),
            "--working-directory",
            str(tmp_path),
            "stack-backends",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    return result, log


@pytest.mark.skipif(_JUST is None, reason="just is required to drive the stack-backends recipe")
def test_stack_up_sources_env_sh_and_honors_its_oidc_image(tmp_path: Path) -> None:
    """The arch branch (#2400): when env.sh resolves `KDIVE_OIDC_IMAGE` — the
    ppc64le-under-qemu case — `stack-backends` must take ADR-0358's pull path without ever probing
    or building the local `kdive-mock-oidc:dev` image. Before the fix, `stack-backends` never
    sourced env.sh, so this auto-detected value never reached the recipe and this assertion
    failed."""
    result, log = _run_stack_up(
        tmp_path, oidc_image="ghcr.io/randomparity/mock-oauth2-server@sha256:test"
    )
    assert result.returncode == 0, result.stderr
    invocations = log.read_text()
    assert "image inspect kdive-mock-oidc:dev" not in invocations
    assert "compose build oidc" not in invocations


@pytest.mark.skipif(_JUST is None, reason="just is required to drive the stack-backends recipe")
def test_stack_up_local_build_path_is_unchanged_when_env_sh_sets_nothing(tmp_path: Path) -> None:
    """#2400 acceptance: "On x86_64 the local-build path is unchanged." When env.sh leaves
    `KDIVE_OIDC_IMAGE` unset, `stack-backends` must still probe for — and build when absent — the
    local `kdive-mock-oidc:dev` image, exactly as before sourcing env.sh was added."""
    result, log = _run_stack_up(tmp_path, oidc_image=None)
    assert result.returncode == 0, result.stderr
    invocations = log.read_text()
    assert "image inspect kdive-mock-oidc:dev" in invocations
    assert "compose build oidc" in invocations


def _run_stack_services(
    tmp_path: Path,
    *args: str,
    seaweedfs_init_status: int = 0,
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Drive the real bring-up script in a fake checkout with a stubbed `docker`.

    `seaweedfs_init_status` is what the stub returns for `run --rm seaweedfs-init`, so a test can
    make the bucket one-shot fail without a store. Returns the result and the invocation log.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    log.touch()
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "{log}"\n'
        '[[ "$1 $2 $3" == "image inspect kdive-mock-oidc:dev" ]] && exit 1\n'
        f'[[ "$*" == *"run --rm seaweedfs-init"* ]] && exit {seaweedfs_init_status}\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    # The script is REAL, so every binary it reaches that is not stubbed here resolves to the
    # host's /usr/bin. Its libvirt block runs `sudo systemctl enable --now virtqemud.socket` on a
    # host where libvirt is unreachable, and `sudo install -d /var/lib/kdive/rootfs` where the
    # provision dirs are absent — a unit test enabling sockets and creating root directories on
    # the operator's machine, silently, because the assertions are about earlier phases. Record
    # and refuse instead of executing, so no stage a caller drives can escape to the host.
    for name in ("sudo", "virsh", "systemctl"):
        refused = bin_dir / name
        refused.write_text(
            f'#!/usr/bin/env bash\nprintf \'REFUSED {name} %s\\n\' "$*" >> "{log}"\nexit 0\n',
            encoding="utf-8",
        )
        refused.chmod(0o755)
    script = _stub_live_stack_scripts(tmp_path, oidc_image=None)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    env.update(env_extra or {})
    result = subprocess.run(
        [str(script), *args], capture_output=True, text=True, check=False, env=env
    )
    return result, log


def test_backends_stage_propagates_the_one_shot_failure(tmp_path: Path) -> None:
    """A non-zero seaweedfs-init fails the stage (ADR-0655).

    The replaced `up -d` form started the one-shot among the services and polled only postgres,
    so a failed bucket creation was silent here and surfaced much later as a worker store check.
    """
    result, _ = _run_stack_services(tmp_path, "--stage", "backends", seaweedfs_init_status=7)
    assert result.returncode != 0, result.stdout


def test_backends_stage_waits_only_on_the_long_running_backends(tmp_path: Path) -> None:
    """`--wait` treats any container exit as a wait failure, so the one-shot stays outside it."""
    result, log = _run_stack_services(tmp_path, "--stage", "backends")
    assert result.returncode == 0, result.stderr
    wait = [ln for ln in log.read_text().splitlines() if "--wait" in ln]
    assert len(wait) == 1, wait
    assert wait[0].endswith("postgres seaweedfs oidc"), wait[0]
    assert "seaweedfs-init" not in wait[0]


def test_backends_stage_runs_neither_obs_nor_the_app_tier_reconcile(tmp_path: Path) -> None:
    """Both are `services` phases: the backends-only path started neither, and the app-tier
    `rm -sf` is destructive against the containerized tier."""
    result, log = _run_stack_services(tmp_path, "--stage", "backends")
    assert result.returncode == 0, result.stderr
    invocations = log.read_text()
    assert "rm -sf migrate server worker reconciler" not in invocations
    assert "--profile obs" not in invocations
    assert "role-bootstrap" not in invocations


def test_backends_stage_is_inert_about_skip_obs(tmp_path: Path) -> None:
    """KDIVE_SKIP_OBS defaults --skip-obs, so rejecting it would fail
    `KDIVE_SKIP_OBS=1 just stack-backends` over a flag the operator never passed."""
    result, _ = _run_stack_services(
        tmp_path, "--stage", "backends", env_extra={"KDIVE_SKIP_OBS": "1"}
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("args", "expected"),
    (
        (("--stage", "nonsense"), "unknown --stage"),
        (("--stage", "backends", "--skip-libvirt"), "does not reach the phase"),
        (("--stage", "backends", "--reset-db"), "does not reach the phase"),
    ),
    ids=("unknown-stage", "skip-libvirt", "reset-db"),
)
def test_backends_stage_rejects_flags_it_cannot_reach(
    tmp_path: Path, args: tuple[str, ...], expected: str
) -> None:
    result, _ = _run_stack_services(tmp_path, *args)
    assert result.returncode == 2, result.stdout
    assert expected in result.stderr


def test_stack_backends_recipe_delegates_to_the_script() -> None:
    """One implementation of the backend readiness contract, not two (ADR-0655)."""
    body = _JUSTFILE.read_text(encoding="utf-8").split("\nstack-backends:\n", 1)[1]
    body = body.split("\n\n", 1)[0]
    assert "stack-services.sh --stage backends" in body
    assert "docker compose" not in body


def test_services_stage_reconciles_the_app_tier(tmp_path: Path) -> None:
    """The default stage still removes any compose app-tier container before starting hosts.

    Task 2 moved that `rm -sf` behind a stage gate, and the text guard in
    tests/live_stack/test_up_invariants.py cannot see which condition it sits under — a gate that
    is never true would leave a compose `server` holding port 8000 against the host process, whose
    symptom is a 401 that reads as an auth bug. The run fails later on the fake checkout's absent
    worker-lifecycle.sh; what is asserted is what reached `docker` before that.
    """
    _, log = _run_stack_services(tmp_path, "--stage", "services", "--skip-libvirt")
    recorded = log.read_text()
    assert "rm -sf migrate server worker reconciler" in recorded
    # The reconcile is the contract; this is the guard on the harness itself. --skip-libvirt is
    # legal under --stage services (only --stage backends rejects it), so the stage gate is still
    # exercised while the privileged block stays unreached.
    assert "REFUSED" not in recorded, f"bring-up attempted a privileged call: {recorded}"


@pytest.mark.parametrize("operation", ("status", "stop", "diagnostics", "recover"))
def test_lifecycle_client_dispatches_every_argument_free_operation(operation: str) -> None:
    """The shell `case` and the usage line must both agree with the wire grammar.

    `worker-lifecycle.sh` is the only client an operator runs, so an operation the contract
    accepts but the `case` does not name falls through to `*)` and exits 2 on usage before a
    request is ever built -- indistinguishable from a typo. Asserting the exit status cannot
    catch that, because a bogus argument produces exactly the same status and the same usage
    line; only the arm patterns themselves separate the two.
    """
    lifecycle = LIFECYCLE.read_text()
    dispatch = lifecycle.split('case "${1:-}" in', 1)[1]
    arms: dict[frozenset[str], str] = {}
    for block in dispatch.split(";;"):
        header, _, body = block.partition(")")
        patterns = frozenset(pattern.strip() for pattern in header.strip().split("|"))
        if patterns:
            arms[patterns] = body
    named = {pattern for patterns in arms for pattern in patterns}

    assert operation in named, named
    assert "*" in named, "the fall-through arm must still reject an unknown argument"
    assert f"|{operation}" in lifecycle.split("usage()", 1)[1].split("\n}", 1)[0]

    # Naming the operation in an arm is not enough: the arm must build a request rather than
    # fall back to usage, which would be indistinguishable from the `*)` arm at the exit status.
    body = next(body for patterns, body in arms.items() if operation in patterns)
    assert 'request "$1"' in body, body


def test_lifecycle_diagnostics_alone_skips_the_compatibility_probe() -> None:
    """Recovery must fail closed on a skewed host; only `diagnostics` stays reachable.

    A host is reprovisioned *before* `recover` is dispatched against it, so exempting `recover`
    from the probe would let it run against a venv whose coordinator has no `recover` method —
    an `internal_error` from an `AttributeError`, not the actionable skew message.
    """
    lifecycle = LIFECYCLE.read_text()

    assert '[[ "$operation" != diagnostics ]]' in lifecycle
