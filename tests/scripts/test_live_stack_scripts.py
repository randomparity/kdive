import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from kdive.config.external_env import EXTERNAL_ENV_VARS
from kdive.health.aux_bind import PROCESS_DEFAULT_PORTS

ROOT = Path(__file__).resolve().parents[2]


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


@pytest.mark.skipif(os.geteuid() == 0, reason="the self-owned arm needs a non-root caller")
def test_surplus_worker_remedy_uses_supported_teardown_for_self_owned_workers() -> None:
    """The remedy delegates ownership handling to the supported teardown path (#1733).

    Under `KDIVE_WORKER_AS_ROOT=0` the workers are the operator's own processes, so a `sudo kill`
    is wrong — and on a host where the operator is already root with no sudo installed it is not
    even runnable. Stand in two self-owned processes for the worker scan and drive the surplus
    branch: the remedy it prints must be a bare `kill -9` naming both pids.
    """
    result = _lib(
        "sleep 30 & first=$!\n"
        "sleep 30 & second=$!\n"
        'worker_pids() { echo "$first"; echo "$second"; }\n'
        "require_workers_alive 1\n"
        "status=$?\n"
        'kill "$first" "$second" 2>/dev/null\n'
        'echo "PIDS=$first,$second"\n'
        "exit $status"
    )
    assert result.returncode != 0, "two live workers against a want of 1 is a surplus"
    first, second = result.stdout.split("PIDS=")[1].strip().split(",")
    assert f"Live worker pids: {first} {second}" in result.stderr
    assert "\n    scripts/live-stack/down.sh --force\n" in result.stderr
    assert "kill -9" not in result.stderr, "manual privilege selection is no longer the remedy"


@pytest.mark.skipif(os.geteuid() == 0, reason="a root caller needs sudo for nothing")
def test_surplus_worker_remedy_uses_supported_teardown_for_foreign_workers() -> None:
    """The same supported command handles a mixed-ownership pid set (#1733).

    The complement of the self-owned arm, and the reason the test is "is any pid NOT mine" rather
    than "are they all mine": a bare `kill -9` against a pid the operator cannot signal fails with
    EPERM and no guidance. pid 1 is root-owned on any host this runs on, so pairing it with this
    shell's own pid exercises the mixed set — the shape a stack that switched
    `KDIVE_WORKER_AS_ROOT` between runs actually leaves behind.
    """
    result = _lib("worker_pids() { echo 1; echo $$; }\nrequire_workers_alive 1\n")
    assert result.returncode != 0, "two live workers against a want of 1 is a surplus"
    assert "\n    scripts/live-stack/down.sh --force\n" in result.stderr
    assert "sudo kill -9" not in result.stderr


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


def test_extra_workers_get_health_ports_clear_of_the_registered_defaults() -> None:
    """Worker 1 keeps the process default; extras must not land on ANOTHER process's port.

    uvicorn's bind is exclusive, so an extra worker that reused 9465 — or stepped up onto the
    reconciler's 9466 — would die at startup instead of claiming jobs, and the multi-worker stack
    would silently degrade back to the single-worker serialization this knob exists to escape.
    """
    assert _lib("extra_worker_health_bind 1").stdout == "", "worker 1 must keep the default bind"
    ports = {
        int(_lib(f"extra_worker_health_bind {index}").stdout.rsplit(":", 1)[1])
        for index in (2, 3, 4)
    }
    assert len(ports) == 3, f"each extra worker needs its own port, got {ports}"
    assert not ports & set(PROCESS_DEFAULT_PORTS.values()), (
        f"extra-worker ports {ports} collide with the registered defaults {PROCESS_DEFAULT_PORTS}"
    )


def test_multiple_workers_refuse_an_explicit_health_bind() -> None:
    """An explicit bind wins for EVERY process, so it cannot coexist with more than one worker.

    Both accommodations are wrong: honouring the operator's port walks the extras onto the
    registered server and reconciler defaults, and ignoring it silently discards the setting for
    every worker but the first. Neither yields a stack that comes up, so bring-up must refuse and
    name the knob to drop rather than start workers that die on an exclusive bind.
    """
    result = _lib(
        'py="/nonexistent/python"\nrestart_host_processes\n',
        KDIVE_WORKER_COUNT="2",
        KDIVE_HEALTH_BIND_ADDR="127.0.0.1:9500",
    )
    assert result.returncode != 0, "the combination must be refused"
    assert "KDIVE_HEALTH_BIND_ADDR" in result.stderr, result.stderr
    assert "Unset KDIVE_HEALTH_BIND_ADDR" in result.stderr, "the message must name the remedy"
    # One worker with an explicit bind is the pre-existing single-process case and stays allowed:
    # it must fail later (on the stub interpreter), not on this guard.
    single = _lib(
        'py="/nonexistent/python"\nrestart_host_processes\n',
        KDIVE_WORKER_COUNT="1",
        KDIVE_HEALTH_BIND_ADDR="127.0.0.1:9500",
    )
    assert "KDIVE_HEALTH_BIND_ADDR" not in single.stderr, single.stderr


def test_worker_log_paths_are_distinct_and_keep_the_first_unsuffixed() -> None:
    """Worker 1 keeps the name recorded runbooks and proof records already cite."""
    root_logs = [_lib(f"worker_log_path {i}", KDIVE_WORKER_AS_ROOT="1").stdout for i in (1, 2, 3)]
    user_logs = [_lib(f"worker_log_path {i}", KDIVE_WORKER_AS_ROOT="0").stdout for i in (1, 2, 3)]
    assert root_logs[0].endswith("/worker-root.log")
    assert user_logs[0].endswith("/worker.log")
    assert len(set(root_logs)) == 3, root_logs
    assert len(set(user_logs)) == 3, user_logs


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


def test_build_stamps_report_one_row_per_worker_log(tmp_path: Path) -> None:
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
    assert "build stamps" in header and "worker process(es) live" in header, header
    labels = [line.split()[0] for line in rows[1:] if line.startswith("  ")]
    assert labels == ["server", "reconciler", "worker-root", "worker-root-2"], labels
    # Nothing runs from the stubbed interpreter, so the count exposes both rows as stale logs
    # rather than as graded processes — the direction a file-only enumeration cannot report.
    assert "0 worker process(es) live" in header, header


def test_bring_up_fails_when_a_worker_did_not_come_up(tmp_path: Path) -> None:
    """The settle gate counts a host-wide total, so it cannot see a missing worker on its own.

    `daemon_pids` is deliberately checkout-agnostic, and `stop_daemons` warns rather than fails
    after ten seconds — so a survivor from another worktree makes 2 + N add up while one of THIS
    checkout's workers is dead. Bring-up would exit 0 on a stack that silently serializes, which
    is the whole failure the knob exists to escape. The count must be asserted on workers.
    """
    result = _lib(
        f'py="{tmp_path}/no-such-python"\nlog_dir="{tmp_path}"\nrequire_workers_alive 2\n'
    )
    assert result.returncode != 0, "a shortfall must fail bring-up, not warn"
    assert "asked for 2 worker(s) but only 0" in result.stderr, result.stderr
    # The message must reach the aux-port cause, which is otherwise a silent local port conflict.
    assert "address already in use" in result.stderr, result.stderr
    # An exact count passes.
    assert _lib(f'py="{tmp_path}/no-such-python"\nrequire_workers_alive 0\n').returncode == 0


def test_bring_up_fails_on_a_surplus_worker_too(tmp_path: Path) -> None:
    """A survivor of stop_daemons is from THIS checkout, so `>=` would let it mask a dead worker.

    stop_daemons warns and returns 0 after ten seconds, and a worker ignores SIGTERM until its
    job ends — which the contention arm arranges by parking workers inside a multi-GiB fetch. So
    a leftover worker is the expected state here, not an edge case, and it may be running older
    code. The two directions need different remedies, so the surplus must fail on its own.
    """
    surplus = _lib(
        f'py="{tmp_path}/no-such-python"\nworker_pids() {{ echo 111; echo 222; }}\n'
        "require_workers_alive 1\n"
    )
    assert surplus.returncode != 0, "a surplus must fail, not pass as 'at least enough'"
    assert "but 2 from this checkout are running" in surplus.stderr, surplus.stderr
    assert "outlived stop_daemons" in surplus.stderr, "the surplus remedy differs from a shortfall"
    assert "111" in surplus.stderr and "222" in surplus.stderr, "name the pids to stop"


def test_the_surplus_remedy_is_one_that_actually_clears_the_surplus(tmp_path: Path) -> None:
    """The remedy must use forced teardown, which can end the worker this message is about.

    Plain teardown remains graceful-only. The explicit force option escalates after that grace
    period without asking the operator to reproduce pid discovery and privilege handling.
    """
    surplus = _lib(
        f'py="{tmp_path}/no-such-python"\nworker_pids() {{ echo 111; echo 222; }}\n'
        "require_workers_alive 1\n"
    )
    assert surplus.returncode != 0
    assert "scripts/live-stack/down.sh --force" in surplus.stderr, (
        f"the remedy must name the supported forced teardown path: {surplus.stderr}"
    )
    # Not a bare `"wait" in stderr` — the pre-fix message already said "the ten-second wait only
    # warns", so that substring passes against the very message this test exists to reject.
    assert surplus.stderr.index("wait for the in-flight job") < surplus.stderr.index("--force"), (
        f"the non-destructive option must be offered before the destructive one: {surplus.stderr}"
    )
    # Killing abandons a running job, so the message must not stop at the command: it has to say
    # what picks up the pieces, or the operator is left guessing whether they have to wipe. The
    # mechanism is the queue, NOT the reconciler — `dequeue` reclaims a `running` row whose lease
    # has lapsed and charges an attempt. The message deliberately claims nothing beyond that: two
    # earlier drafts asserted a downstream cleanup path the source contradicted, so the scope of
    # what it promises is itself the thing under test.
    # The clause spans the message's line wrap, so compare against a whitespace-normalized copy:
    # the assertion is that the sentence is present and in order, not that the wrap falls anywhere
    # in particular. Pinning the wrap point makes an unrelated rewording redden this for no reason.
    assert "another worker reclaims each one once its lease lapses" in " ".join(
        surplus.stderr.split()
    ), f"the consequence of kill -9 and what recovers it must be stated: {surplus.stderr}"
    # The pid list is every worker sharing this interpreter, not only the survivor — telling the
    # operator otherwise sends them to kill -9 a set the prose has mislabelled.
    assert "INCLUDING the ones this run started" in surplus.stderr, surplus.stderr


def test_the_surplus_report_scans_the_process_table_exactly_once(tmp_path: Path) -> None:
    """One scan, or the count and the pid list the operator is told to kill can disagree.

    Counting with one `ps` and printing with a second lets a worker exit in between, so the
    message reports a count its own pid list contradicts — and that list is what the remedy
    tells the operator to act on. The stub here returns two pids to the first scan and one to
    every scan after it, which is exactly that interleaving.
    """
    counter = tmp_path / "scans"
    surplus = _lib(
        f'py="{tmp_path}/no-such-python"\n'
        f'worker_pids() {{ echo scan >>"{counter}"\n'
        f'  if (( $(wc -l <"{counter}") == 1 )); then printf "111\\n222\\n"; else echo 111; fi\n'
        "}\n"
        "require_workers_alive 1\n"
    )
    assert surplus.returncode != 0
    assert counter.read_text().count("scan") == 1, (
        f"the process table must be scanned once, not once per use: {counter.read_text()!r}"
    )
    # The consequence of the single scan: count and pid list still agree after the table moved.
    assert "but 2 from this checkout are running" in surplus.stderr, surplus.stderr
    assert "222" in surplus.stderr, (
        f"the pid list must match the count it reported: {surplus.stderr}"
    )


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


def test_surplus_report_distinguishes_a_pid_that_was_not_signalled(tmp_path: Path) -> None:
    """Waiting is not offered when the preceding SIGTERM never reached a worker."""
    result = _lib(
        f'py="{tmp_path}/no-such-python"\n'
        "STOP_DAEMONS_UNSIGNALLED=(111)\n"
        "worker_pids() { echo 111; echo 222; }\n"
        "require_workers_alive 1\n"
    )
    assert result.returncode != 0
    assert "SIGTERM was not delivered to: 111" in result.stderr
    assert "Waiting cannot end those pids" in " ".join(result.stderr.split())
    assert "wait for the in-flight job" not in result.stderr


def test_surplus_report_ignores_unsignalled_nonworker_daemons(tmp_path: Path) -> None:
    """Host-wide stop failures must not change advice for checkout-scoped worker pids."""
    result = _lib(
        f'py="{tmp_path}/no-such-python"\n'
        "STOP_DAEMONS_UNSIGNALLED=(999)\n"
        "worker_pids() { echo 111; echo 222; }\n"
        "require_workers_alive 1\n"
    )
    assert result.returncode != 0
    assert "SIGTERM was not delivered" not in result.stderr
    assert "wait for the in-flight job" in result.stderr


def test_build_stamps_still_report_a_worker_row_with_no_logs(tmp_path: Path) -> None:
    """An empty log dir must still print a worker row, not silently drop the process."""
    rows = _build_stamps(tmp_path, {})
    labels = [line.split()[0] for line in rows[1:] if line.startswith("  ")]
    assert labels == ["server", "reconciler", "worker"], labels
    assert all("<no startup log line>" in line for line in rows[1:]), rows


def test_worker_pids_are_scoped_to_this_checkout(tmp_path: Path) -> None:
    """A worker from a sibling worktree must not inflate the live count.

    Several worktrees run on this host, and `stop_daemons` deliberately matches every checkout's
    daemons. Counting them here too would let a foreign worker mask a stale log — the blindness
    the header exists to expose — so this matcher is anchored on the resolved interpreter.

    Runs a REAL process whose argv is exactly `<venv>/bin/python -m kdive worker`, against a stub
    `kdive` package that just sleeps, so the matcher is exercised rather than the source text.
    """
    root = tmp_path / "other"
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.symlink_to(sys.executable)
    stub_pkg = root / "kdive"
    stub_pkg.mkdir()
    (stub_pkg / "__init__.py").write_text("")
    (stub_pkg / "__main__.py").write_text("import time\ntime.sleep(120)\n")

    # A hermetic env: PYTHONPATH pins the stub package, and PYTHONSAFEPATH is cleared because it
    # would drop the cwd `-m` relies on. Inheriting pytest's environment let the real installed
    # kdive answer `-m kdive`, and that process exits on its own once it cannot reach a database.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONHOME")}
    env["PYTHONPATH"] = str(root)

    with subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(python), "-m", "kdive", "worker"], cwd=root, env=env
    ) as proc:
        try:
            deadline = time.monotonic() + 30
            counted = ""
            while time.monotonic() < deadline:
                assert proc.poll() is None, (
                    f"the stub worker exited early (rc={proc.returncode}); `-m kdive` did not "
                    "resolve to the sleeping stub, so the matcher was never exercised"
                )
                counted = _lib(f'py="{python}"\nworker_pids\n').stdout
                if counted.strip():
                    break
                time.sleep(0.1)
            assert str(proc.pid) in counted, (
                f"a worker launched from py={python} must be counted, got {counted!r}"
            )
            other = _lib(f'py="{tmp_path}/elsewhere/.venv/bin/python"\nworker_pids\n').stdout
            assert str(proc.pid) not in other, (
                f"a worker from another checkout must NOT be counted, got {other!r}"
            )
            # ps truncates each line to COLUMNS, and a checkout path plus " -m kdive worker" runs
            # well past 80. Without `-ww` every daemon matcher here finds NOTHING in an ordinary
            # 80-column shell — stop_daemons reports none running and bring-up counts zero alive.
            narrow = _lib(f'py="{python}"\nworker_pids\ndaemon_pids\n', COLUMNS="80").stdout
            assert str(proc.pid) in narrow, (
                f"COLUMNS=80 truncated ps and lost the worker: {narrow!r}"
            )
        finally:
            proc.kill()


def test_restart_host_processes_starts_one_worker_by_default(tmp_path: Path) -> None:
    logs = _start_workers(tmp_path, "1")
    assert [p.name for p in logs] == ["worker.log"]
    assert "health=<unset>" in logs[0].read_text(), "the sole worker keeps the process default"


def test_restart_host_processes_starts_every_configured_worker(tmp_path: Path) -> None:
    """Three workers must really be launched, each with its own log and its own aux port."""
    logs = _start_workers(tmp_path, "3")
    assert [p.name for p in logs] == ["worker-2.log", "worker-3.log", "worker.log"]
    binds = [p.read_text().split("health=")[1].strip() for p in logs]
    assert binds.count("<unset>") == 1, f"exactly one worker keeps the default bind: {binds}"
    explicit = [b for b in binds if b != "<unset>"]
    assert len(set(explicit)) == 2, f"extra workers must not share a bind: {binds}"


def test_root_worker_launch_splices_the_health_bind_after_sourcing_env(tmp_path: Path) -> None:
    """The DEFAULT launch branch is the sudo-root one, and it is what the runbook arm runs.

    The per-worker bind is spliced into a quoted `sudo bash -c` string there, and each extra
    worker must get its OWN bind — a regression that gives them one port degrades a two-worker
    stack back to serialization, which is the failure the whole change exists to remove.

    The ordering assertion below is a cheap guard, not a live constraint: env.sh does not mention
    KDIVE_HEALTH_BIND_ADDR at all today, so nothing re-defaults over the export and either order
    would work. It is asserted so that the export stays the last writer if env.sh ever grows a
    `:-` default for that variable, as it already has for the other forwarded vars.
    """
    sudo_stub = tmp_path / "sudo"
    sudo_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{tmp_path}/sudo-argv"\n')
    sudo_stub.chmod(0o755)
    result = _lib(
        f'py="/nonexistent/python"\n'
        f'log_dir="{tmp_path}/logs"\n'
        'mkdir -p "$log_dir"\n'
        "start_worker 1 builduser /src/linux\n"
        "start_worker 2 builduser /src/linux\n"
        "start_worker 3 builduser /src/linux\n",
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        KDIVE_WORKER_AS_ROOT="1",
        KDIVE_WORKER_DATABASE_URL="postgresql://x/y",
        KDIVE_S3_ENDPOINT_URL="http://x",
    )
    assert result.returncode == 0, result.stderr
    launches = (tmp_path / "sudo-argv").read_text().splitlines()
    assert len(launches) == 3, launches

    assert "KDIVE_HEALTH_BIND_ADDR" not in launches[0], "worker 1 must keep the process default"
    binds = []
    for launch in launches[1:]:
        assert "export KDIVE_HEALTH_BIND_ADDR=" in launch, f"extra worker got no bind: {launch}"
        assert launch.index("source scripts/live-stack/env.sh") < launch.index(
            "export KDIVE_HEALTH_BIND_ADDR="
        ), "the bind export must stay the last writer, after the env.sh source"
        binds.append(launch.split("export KDIVE_HEALTH_BIND_ADDR=")[1].split("'")[1])
    assert len(set(binds)) == 2, f"extra workers must not share a bind: {binds}"
    # Each worker still writes its own log, so an observation is attributable to one process.
    assert len({launch.split(">>")[1] for launch in launches}) == 3, launches


def test_live_stack_env_exports_required_defaults() -> None:
    env = (ROOT / "scripts/live-stack/env.sh").read_text()
    required = [
        # One export per host database authority (#1929); no shared DSN default remains.
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
    for name in (
        "env.sh",
        "apply-migrations.sh",
        "up.sh",
        "down.sh",
        "status.sh",
    ):
        text = (ROOT / "scripts/live-stack" / name).read_text()
        assert text.startswith("#!/usr/bin/env bash\n"), f"{name}: missing bash shebang"
        assert "\nset -euo pipefail\n" in text, f"{name}: missing 'set -euo pipefail'"


def test_restart_host_processes_starts_all_three() -> None:
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert "restart_host_processes" in text
    assert "-m kdive server" in text
    assert "-m kdive reconciler" in text
    assert "-m kdive worker" in text


def test_sudo_root_worker_forwards_backend_endpoints() -> None:
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    sudo_block = text[text.index("sudo bash -c") : text.index("-m kdive worker >>")]
    assert "KDIVE_WORKER_DATABASE_URL='${KDIVE_WORKER_DATABASE_URL}'" in sudo_block
    assert r"KDIVE_DATABASE_URL=\"\${KDIVE_WORKER_DATABASE_URL}\"" in sudo_block
    assert (
        "unset KDIVE_MIGRATION_DATABASE_URL KDIVE_SERVER_DATABASE_URL "
        "KDIVE_RECONCILER_DATABASE_URL" in sudo_block
    )
    assert "KDIVE_S3_ENDPOINT_URL='${KDIVE_S3_ENDPOINT_URL}'" in sudo_block


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


def test_role_database_dsns_are_never_env_program_arguments() -> None:
    for relative_path in (
        "scripts/live-stack/apply-migrations.sh",
        "scripts/live-stack/lib.sh",
        "scripts/live-stack/status.sh",
        "scripts/live-stack/up.sh",
    ):
        logical_lines = (ROOT / relative_path).read_text(encoding="utf-8").replace("\\\n", " ")
        for line in logical_lines.splitlines():
            # `env.sh` must not read as an `env` invocation: the look-ahead excludes a name that
            # continues into a path/component, so only a real `env` program call can match.
            if re.search(r"\benv(?![.\w-]).*DATABASE_URL=", line):
                pytest.fail(f"database DSN exposed in env argv: {relative_path}: {line.strip()}")


def test_status_database_probe_scrubs_unrelated_role_dsns(tmp_path: Path) -> None:
    status = tmp_path / "status.sh"
    source = (ROOT / "scripts/live-stack/status.sh").read_text()
    setup = source[: source.index('echo "=== compose')]
    database = source[source.index('echo "=== database') : source.index('echo "=== libvirt')]
    status.write_text(setup + database + "exit 0\n", encoding="utf-8")
    for name in ("lib.sh", "env.sh"):
        (tmp_path / name).write_text(
            (ROOT / "scripts/live-stack" / name).read_text(), encoding="utf-8"
        )
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


def test_host_daemon_children_receive_only_their_role_database_authority(tmp_path: Path) -> None:
    """Each spawned daemon sees exactly one authority: its own role's member DSN (#1929)."""
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
        f'py="{python}"\n'
        f'log_dir="{tmp_path / "logs"}"\n'
        "stop_daemons() { :; }\n"
        "require_free_http_port() { :; }\n"
        "wait_for_daemons_to_settle() { :; }\n"
        "require_workers_alive() { :; }\n"
        "restart_host_processes\n",
        KDIVE_WORKER_COUNT="1",
        KDIVE_WORKER_AS_ROOT="0",
        KDIVE_DAEMON_PROBE=str(probe),
        KDIVE_MIGRATION_DATABASE_URL="migration-canary",
        KDIVE_SERVER_DATABASE_URL="server-canary",
        KDIVE_WORKER_DATABASE_URL="worker-canary",
        KDIVE_RECONCILER_DATABASE_URL="reconciler-canary",
    )
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (
        not probe.exists() or len(probe.read_text(encoding="utf-8").splitlines()) < 3
    ):
        time.sleep(0.05)
    rows = set(probe.read_text(encoding="utf-8").splitlines())
    assert rows == {
        "-m kdive server|server-canary|<missing>|server-canary|<missing>|<missing>",
        "-m kdive reconciler|reconciler-canary|<missing>|<missing>|<missing>|reconciler-canary",
        "-m kdive worker|worker-canary|<missing>|<missing>|worker-canary|<missing>",
    }


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
