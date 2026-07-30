import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

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
    log directory, and the three helpers that touch the live process table — so the loop itself,
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
    reported a *surplus*. The ceiling must therefore bound BOTH ends, not just the upper one.
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
    """The remedy must not be `down.sh`, which cannot end the worker this message is about.

    `down.sh` calls the same `stop_daemons` that just let the survivor through: one SIGTERM, a
    ten-second poll, a WARN, `return 0` — there is no escalation past SIGTERM anywhere. So a
    worker parked in a long job (the message's own scenario, and the one the contention arm
    manufactures deliberately) survives `down.sh` exactly as it survived bring-up, and the
    compose backends get torn down for nothing. `--yes` does not change that either: it gates
    only the `--wipe` confirmation prompt, so `down.sh --yes` is `down.sh`.

    The two remedies that do work are waiting for the job to end, and ending the pids the
    message already prints. Both must be named, and the ineffective one must be gone.
    """
    surplus = _lib(
        f'py="{tmp_path}/no-such-python"\nworker_pids() {{ echo 111; echo 222; }}\n'
        "require_workers_alive 1\n"
    )
    assert surplus.returncode != 0
    # Scoped to the prescription, not to the string: `down.sh --wipe` is named further down as
    # what forces a leaked VM's System to torn_down, which is true and useful. What must be gone
    # is `down.sh --yes` offered as the way to clear the surplus.
    assert "down.sh --yes" not in surplus.stderr, (
        f"down.sh cannot clear a SIGTERM-ignoring worker; it must not be the remedy: "
        f"{surplus.stderr}"
    )
    assert "Tearing the stack down will NOT clear it" in surplus.stderr, (
        f"the message must say outright that teardown does not fix this: {surplus.stderr}"
    )
    assert "kill -9 111 222" in surplus.stderr, (
        f"the remedy must name the pids it just printed: {surplus.stderr}"
    )
    # Not a bare `"wait" in stderr` — the pre-fix message already said "the ten-second wait only
    # warns", so that substring passes against the very message this test exists to reject.
    assert surplus.stderr.index("wait for the in-flight job") < surplus.stderr.index("kill -9"), (
        f"the non-destructive option must be offered before the destructive one: {surplus.stderr}"
    )
    # Killing abandons a running job, so the message must not stop at the command: it has to say
    # what picks up the pieces, or the operator is left guessing whether they have to wipe. The
    # mechanism is the queue, NOT the reconciler — `dequeue` reclaims a `running` row whose
    # lease has lapsed and charges an attempt; the reconciler's leaked-domain pass is gated off
    # while a non-torn_down System row exists, so it does not clean this up.
    assert "another worker reclaims it once its lease" in surplus.stderr, (
        f"the consequence of kill -9 and what recovers it must be stated: {surplus.stderr}"
    )
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

    `kill` and `sleep` are stubbed because this function really signals pids and really waits ten
    seconds — 111/222 could belong to someone on this host.
    """
    counter = tmp_path / "scans"
    result = _lib(
        "kill() { :; }\n"
        "sleep() { :; }\n"
        f'daemon_pids() {{ echo scan >>"{counter}"\n'
        f'  if (( $(wc -l <"{counter}") > 21 )); then echo 999; else printf "111\\n222\\n"; fi\n'
        "}\n"
        "stop_daemons\n"
    )
    # One scan builds the kill list, then the poll loop runs 20 times. A 22nd means the WARN
    # went back to `ps` instead of reusing what the loop had already read.
    assert counter.read_text().count("scan") == 21, counter.read_text().count("scan")
    assert "still running after stop: 111 222" in result.stderr, result.stderr
    assert "999" not in result.stderr, (
        f"the WARN re-scanned after the poll loop instead of reusing it: {result.stderr}"
    )


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
        KDIVE_DATABASE_URL="postgresql://x/y",
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
        "KDIVE_DATABASE_URL",
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


def test_restart_host_processes_starts_all_three() -> None:
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert "restart_host_processes" in text
    assert "-m kdive server" in text
    assert "-m kdive reconciler" in text
    assert "-m kdive worker" in text


def test_sudo_root_worker_forwards_backend_endpoints() -> None:
    # sudo resets the environment, so the root worker re-sources env.sh and would re-default any
    # relocated backend port. The resolved DB + S3 endpoints must be forwarded into the sudo shell
    # so a KDIVE_POSTGRES_PORT/KDIVE_MINIO_PORT override reaches the worker, not just the same-user
    # server/reconciler. The forward must appear inside the `sudo bash -c` block.
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    sudo_block = text[text.index("sudo bash -c") : text.index("-m kdive worker >>")]
    assert "KDIVE_DATABASE_URL='${KDIVE_DATABASE_URL}'" in sudo_block
    assert "KDIVE_S3_ENDPOINT_URL='${KDIVE_S3_ENDPOINT_URL}'" in sudo_block


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
