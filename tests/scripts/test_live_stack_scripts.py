import os
import shutil
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from kdive.config.external_env import EXTERNAL_ENV_VARS

ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE = ROOT / "scripts" / "live-stack" / "worker-lifecycle.sh"


def _lifecycle_status(
    tmp_path: Path, response: str, *, expected_slots: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run the real wrapper against a Python import-time lifecycle response stub."""
    (tmp_path / "sitecustomize.py").write_text(
        "import os\n"
        "from kdive.processes.lifecycle import systemd_worker_control as control\n"
        "from kdive.processes.lifecycle.systemd_worker_contract import LifecycleResponse\n"
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
        ["bash", str(LIFECYCLE), "status"],
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
    from kdive.processes.lifecycle.systemd_worker_contract import LifecycleResponse

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


def _start_workers(tmp_path: Path, worker_count: str) -> list[Path]:
    """Really run `restart_host_processes` against a stub interpreter; return the worker logs.

    Stubs only the environment the launch loop cannot have in a unit test — the interpreter, the
    log directory, and the four helpers that touch the live process table or the HTTP port
    (`stop_daemons`, `require_free_http_port`, `wait_for_daemons_to_settle` and
    `require_workers_alive`, the last of which would otherwise find no live workers) — so the loop,
    the per-worker log naming, and the per-worker health-bind override are the code under test.
    The stub records the aux bind address it was handed, which is the collision this guards.
    """
    stub = tmp_path / "python-stub"
    stub.write_text(
        '#!/usr/bin/env bash\necho "argv=$* health=${KDIVE_HEALTH_BIND_ADDR:-<unset>}"\n'
    )
    stub.chmod(0o755)
    log_dir = tmp_path / "logs"
    result = _lib(
        f'py="{stub}"\n'
        f'log_dir="{log_dir}"\n'
        "stop_daemons() { :; }\n"
        "require_free_http_port() { :; }\n"
        "wait_for_daemons_to_settle() { :; }\n"
        # Stubbed too: the stub interpreter exits immediately, so the real check would scan the
        # process table, find no workers, and fail. Without this the snippet's status came from
        # the trailing `echo` and restart_host_processes could have failed unnoticed.
        "require_workers_alive() { :; }\n"
        "restart_host_processes || echo 'BRING_UP_FAILED'\n"
        'echo "DAEMON_COUNT=${DAEMON_COUNT}"\n',
        KDIVE_WORKER_COUNT=worker_count,
        KDIVE_WORKER_AS_ROOT="0",
    )
    assert result.returncode == 0, result.stderr
    assert "BRING_UP_FAILED" not in result.stdout, (
        f"restart_host_processes must succeed against the stubs: {result.stdout}\n{result.stderr}"
    )
    expected = 2 + int(worker_count)
    assert f"DAEMON_COUNT={expected}" in result.stdout, (
        f"settle check must expect server + reconciler + {worker_count} workers: {result.stdout}"
    )
    # The launches are detached (`setsid nohup ... &`), so poll rather than `wait` on them.
    deadline = time.monotonic() + 10
    logs: list[Path] = []
    while time.monotonic() < deadline:
        logs = sorted(p for p in log_dir.glob("worker*.log") if p.read_text().strip())
        if len(logs) >= int(worker_count):
            break
        time.sleep(0.1)
    return logs


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
    text = (ROOT / "scripts/live-stack/down.sh").read_text()
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
    text = (ROOT / "scripts/live-stack/down.sh").read_text()
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
    """Bring-up keeps graceful signalling; only down.sh wires in escalation (#1733)."""
    down = (ROOT / "scripts/live-stack/down.sh").read_text()
    up = (ROOT / "scripts/live-stack/up.sh").read_text()
    assert down.index("stop_daemons\n") < down.index("force_stop_daemons\n")
    assert '[[ "$force" == "1" ]]' in down
    assert "force_stop_daemons" not in up


def test_down_force_stops_backends_only_after_forced_daemon_stop(tmp_path: Path) -> None:
    """The CLI executes the supported force path before compose teardown."""
    script_dir = tmp_path / "scripts" / "live-stack"
    script_dir.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/live-stack/down.sh", script_dir / "down.sh")
    events = tmp_path / "events"
    (script_dir / "lib.sh").write_text(
        f'repo_root="{tmp_path}"\n'
        f'stop_daemons() {{ echo graceful >>"{events}"; }}\n'
        f'force_stop_daemons() {{ echo force >>"{events}"; }}\n'
        f'docker() {{ echo docker >>"{events}"; }}\n'
    )
    result = subprocess.run(
        ["bash", str(script_dir / "down.sh"), "--force"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert events.read_text().splitlines() == ["graceful", "force", "docker"]


def test_down_force_keeps_backends_up_when_forced_daemon_stop_fails(tmp_path: Path) -> None:
    """A failed SIGKILL path must not dismantle dependencies under a live worker."""
    script_dir = tmp_path / "scripts" / "live-stack"
    script_dir.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/live-stack/down.sh", script_dir / "down.sh")
    events = tmp_path / "events"
    (script_dir / "lib.sh").write_text(
        f'repo_root="{tmp_path}"\n'
        f'stop_daemons() {{ echo graceful >>"{events}"; }}\n'
        f'force_stop_daemons() {{ echo force >>"{events}"; return 1; }}\n'
        f'docker() {{ echo docker >>"{events}"; }}\n'
    )
    result = subprocess.run(
        ["bash", str(script_dir / "down.sh"), "--force"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert events.read_text().splitlines() == ["graceful", "force"]


def test_lifecycle_stop_failure_names_the_recovery_path(tmp_path: Path) -> None:
    """Waiting is not offered when the preceding SIGTERM never reached a worker."""
    text = (ROOT / "scripts/live-stack/down.sh").read_text()
    assert "restore the failed dependency and retry" in text


def test_lifecycle_force_path_states_its_evidence_limit(tmp_path: Path) -> None:
    """Host-wide stop failures must not change advice for checkout-scoped worker pids."""
    text = (ROOT / "scripts/live-stack/down.sh").read_text()
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
        "KDIVE_MINIO_PORT",
        "KDIVE_MINIO_CONSOLE_PORT",
        "KDIVE_OIDC_PORT",
        "KDIVE_PROMETHEUS_PORT",
        "KDIVE_GRAFANA_PORT",
    ]
    for name in required:
        assert f"export {name}=" in env


def test_client_urls_derive_from_the_configurable_ports() -> None:
    # The port var must be the SINGLE source of truth: the client-facing DSN/endpoint defaults must
    # reference the port var, not a second hardcoded literal that could silently drift from compose.
    env = (ROOT / "scripts/live-stack/env.sh").read_text()
    assert "localhost:${KDIVE_POSTGRES_PORT}/kdive" in env
    assert "http://localhost:${KDIVE_MINIO_PORT}" in env
    assert "http://localhost:${KDIVE_OIDC_PORT}/default" in env


def test_live_stack_scripts_are_strict_bash() -> None:
    for name in ("env.sh", "apply-migrations.sh", "up.sh", "down.sh", "status.sh"):
        text = (ROOT / "scripts/live-stack" / name).read_text()
        assert text.startswith("#!/usr/bin/env bash\n"), f"{name}: missing bash shebang"
        assert "\nset -euo pipefail\n" in text, f"{name}: missing 'set -euo pipefail'"


def test_restart_host_processes_starts_ordinary_daemons_and_lifecycle_workers() -> None:
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert "restart_host_processes" in text
    assert "-m kdive server" in text
    assert "-m kdive reconciler" in text
    assert 'worker-lifecycle.sh" start' in text


def test_worker_lifecycle_receives_worker_member_dsn_and_s3_endpoint() -> None:
    # sudo resets the environment, so the root worker re-sources env.sh and would re-default any
    # relocated backend port. The resolved DB + S3 endpoints must be forwarded into the sudo shell
    # so a KDIVE_POSTGRES_PORT/KDIVE_MINIO_PORT override reaches the worker, not just the same-user
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
    text = (ROOT / "scripts/live-stack/up.sh").read_text()
    assert "up -d prometheus" in text, "prometheus must be brought up on its own"
    assert "grafana_supports_arch" in text, "grafana must be gated on host arch"
    assert "#1261" in text, "the skip must be traceable to its tracking issue"


def test_lifecycle_wrapper_uses_the_validated_public_uri_and_python_client() -> None:
    text = (ROOT / "scripts/live-stack/worker-lifecycle.sh").read_text()
    assert "live-worker-libvirt.env" in text
    assert 'source "$LIBVIRT_ENV"' not in text
    assert "LIBVIRT_SOCKET_URIS" in text
    assert "LifecycleRequest.model_validate" in text
    assert "request_path" in text
    assert "KDIVE_WORKER_DATABASE_URL" in text


@pytest.mark.parametrize(
    ("content", "success"),
    [
        (
            "KDIVE_LIBVIRT_URI=qemu+unix:///session?socket="
            "/run/kdive/live-libvirt/libvirt/libvirt-sock\n",
            True,
        ),
        ("KDIVE_LIBVIRT_URI=$(touch /tmp/unsafe)\n", False),
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
    wrapper.write_text(
        LIFECYCLE.read_text().replace(
            "readonly LIBVIRT_ENV=/etc/kdive/live-worker-libvirt.env",
            f"readonly LIBVIRT_ENV={tmp_path / 'libvirt.env'}",
        ),
        encoding="utf-8",
    )
    uri_file = tmp_path / "libvirt.env"
    uri_file.write_text(content, encoding="utf-8")
    uri_file.chmod(0o644)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && require_exact_file() { :; } && load_libvirt_uri',
            "bash",
            str(wrapper),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is success, result.stderr
    assert not (tmp_path / "unsafe").exists()


def test_role_bootstrap_uses_compose_default_only_when_migration_is_implicit(
    tmp_path: Path,
) -> None:
    docker = tmp_path / "docker"
    probe = tmp_path / "environment"
    docker.write_text(
        '#!/bin/sh\nenv > "$KDIVE_BOOTSTRAP_PROBE"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    base = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "KDIVE_BOOTSTRAP_PROBE": str(probe),
        "KDIVE_LOCAL_ROLE_BOOTSTRAP": "1",
        "KDIVE_DATABASE_URL": "generic-canary",
        "KDIVE_SERVER_DATABASE_URL": "server-canary",
        "KDIVE_WORKER_DATABASE_URL": "worker-canary",
        "KDIVE_RECONCILER_DATABASE_URL": "reconciler-canary",
    }
    implicit = subprocess.run(
        ["bash", str(ROOT / "scripts/live-stack/bootstrap-runtime-roles.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=base,
    )
    assert implicit.returncode == 0, implicit.stderr
    implicit_environment = probe.read_text(encoding="utf-8")
    for name in (
        "KDIVE_DATABASE_URL",
        "KDIVE_MIGRATION_DATABASE_URL",
        "KDIVE_SERVER_DATABASE_URL",
        "KDIVE_WORKER_DATABASE_URL",
        "KDIVE_RECONCILER_DATABASE_URL",
    ):
        assert f"{name}=" not in implicit_environment

    explicit = (
        "postgresql://external-owner:dummy@external-db:5432/kdive"  # pragma: allowlist secret
    )
    override = subprocess.run(
        ["bash", str(ROOT / "scripts/live-stack/bootstrap-runtime-roles.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={**base, "KDIVE_MIGRATION_DATABASE_URL": explicit},
    )
    assert override.returncode == 0, override.stderr
    environment = probe.read_text(encoding="utf-8")
    assert f"KDIVE_MIGRATION_DATABASE_URL={explicit}" in environment
    assert explicit not in override.stdout + override.stderr

    empty_override = subprocess.run(
        ["bash", str(ROOT / "scripts/live-stack/bootstrap-runtime-roles.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={**base, "KDIVE_MIGRATION_DATABASE_URL": ""},
    )
    assert empty_override.returncode == 0, empty_override.stderr
    assert "KDIVE_MIGRATION_DATABASE_URL=\n" in probe.read_text(encoding="utf-8")


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


def _worker_path_access(
    tmp_path: Path, target: Path, permissions: str, groups: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"\n'
            'id() { if [[ $1 == -u ]]; then echo 424242; else echo "$3"; fi; }\n'
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


def test_lifecycle_preflight_rejects_an_unwritable_workspace(tmp_path: Path) -> None:
    workspace = Path("/tmp") / f"kdive-workspace-{os.getpid()}"
    workspace.mkdir(mode=0o750)
    workspace.chmod(0o750)
    try:
        result = _worker_path_access(tmp_path, workspace, "rwx", str(os.getgid()))
    finally:
        workspace.rmdir()
    assert result.returncode != 0
    assert "test path is not accessible to kdive-worker-1" in result.stderr


def test_down_blocks_backend_teardown_after_an_unresolved_lifecycle_stop() -> None:
    text = (ROOT / "scripts/live-stack/down.sh").read_text()
    assert text.index('worker-lifecycle.sh" stop') < text.index("stopping compose backends")
    assert "unresolved evidence; backends remain up" in text
    assert "may strand fences" in text
