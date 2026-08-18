"""Guard: CI's apt installs are bounded and retried, and every workflow job declares a timeout.

Two guarantees, one record (ADR-0566), because one is why the other exists. The apt half is
bounded, retried and written down exactly once. The job half — the `declares_a_timeout` test
below — began as its backstop and is now the repo-wide rule for every workflow, apt or not
(#1983); a workflow author who trips a red check from this file is most likely tripping that.

`Install libvirt build headers` wedged for 13 and 33 minutes on two runs in one afternoon
against a ~15s normal (#1978), and each time a human had to notice and cancel the job. The
failure is a **stall**, not a non-zero exit — `apt-get` never returned — so the load-bearing
half of the fix is the hard `timeout` in `scripts/apt-install.sh`, and the retry is what keeps a
timeout from being fatal on a merely slow mirror.

Five of these tests are static: they read the workflows, the script and the `justfile` and
assert the wiring. The other five actually **run** the script — against a stub `apt-get` that
hangs, that fails, and that succeeds, plus a malformed budget — because a wiring assertion
cannot tell whether the timeout fires. That is the whole claim, and asserting the absence of a
bare `apt-get` would pass just as happily over a script that hangs forever. Those runs also
record the argv every stub was handed, so the options that make the bound work —
`--kill-after`, `sudo`, `DPkg::Use-Pty=0` — are under test rather than merely present in the
file.

Stdlib, `pyyaml` and pytest: this reads the tree and runs one shell script, not the project.
`pyyaml` is a declared dependency and is how `tests/scripts/test_live_workflow_shape.py` already
reads these same workflow files — the job-level checks below need a real parser, because a
regex that recognises a job key by its line shape cannot see one carrying a trailing comment,
and reports it as bounded. Everything the *script* is tested with stays stdlib.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_APT_SCRIPT = _ROOT / "scripts" / "apt-install.sh"
_PULL_SCRIPT = _ROOT / "scripts" / "pull-test-images.sh"
_JUSTFILE = _ROOT / "justfile"
_WORKFLOWS = _ROOT / ".github" / "workflows"

#: Every workflow known to install system packages. A *minimum*, not the whole check: the
#: bare-apt ban below runs over every workflow file, so a new one is covered without editing
#: this tuple. This exists so silently losing a call site is also caught.
_APT_WORKFLOWS = ("ci.yml", "test-ordering.yml", "mcp-spec-drift.yml", "live.yml")

#: A `run:` value that installs or refreshes packages, wherever it sits on the line — `run: |`
#: block scalars and the `run: sudo apt-get install …` one-liner alike. Anchoring at the start of
#: the line, the obvious way to write this, matches only the block form and lets the more common
#: one-liner straight through. The `(?:-[^ \t]+[ \t]+)*` is the same class of hole one level in:
#: without it `apt-get -y install` and `apt-get -qq update` walk past a guard that claims to ban
#: them. Comments are stripped before matching rather than excluded by a lookahead, which would
#: only skip *whole-line* comments and still trip on a trailing `# … apt-get install …`.
_BARE_APT = re.compile(
    r"^.*\bapt(?:-get)?[ \t]+(?:-[^ \t]+[ \t]+)*"
    r"(?:install|update|upgrade|dist-upgrade|full-upgrade)\b.*$",
    re.MULTILINE,
)

#: A `#` comment to end of line. YAML has no block comments, so this is the whole grammar.
_YAML_COMMENT = re.compile(r"#.*$", re.MULTILINE)

#: Both spellings GitHub accepts. Globbing only `*.yml` would let a `.yaml` workflow escape.
_WORKFLOW_GLOBS = ("*.yml", "*.yaml")

#: The shared script, however a workflow spells the path to it.
_SHARED_SCRIPT = re.compile(r"\./scripts/apt-install\.sh\b")

#: `readonly BACKOFF_S=(5 15)` in either script.
_BACKOFF = re.compile(r"^(?:readonly\s+)?BACKOFF_S=\((?P<body>[^)]*)\)", re.MULTILINE)

#: `readonly ATTEMPTS=$((${#BACKOFF_S[@]} + 1))` — derived, never a second literal.
_DERIVED_ATTEMPTS = re.compile(
    r"^(?:readonly\s+)?ATTEMPTS=\$\(\(\$\{#BACKOFF_S\[@\]\}\s*\+\s*1\)\)", re.MULTILINE
)

#: The GitHub Actions default. Declaring it bounds nothing — 360 *is* the default this guard
#: exists to close — and that holds on every runner, which is why the comparison is `>=`.
#: (It is also the hosted-runner execution ceiling, so a hosted job cannot enforce more. That
#: half is not universal: `live.yml`'s `native` job is self-hosted, where a larger value would
#: be enforced. It is at 90 today, so nothing turns on it; the reason above is the one that
#: does.)
_ACTIONS_DEFAULT_TIMEOUT_MINUTES = 360

#: `apt-install +PACKAGES:` and the body line that runs the script.
_JUST_RECIPE = re.compile(
    r"^apt-install\s+\+PACKAGES:\s*$\n(?P<body>(?:^[ \t]+.*$\n?)+)", re.MULTILINE
)


def _jobs(path: Path) -> dict[str, object]:
    """Parse a workflow into ``{job name: that job's mapping}``.

    The values are typed `object`, not `dict`, because nothing here has validated them — a
    malformed workflow can put anything under a job key, and claiming `dict` would move that
    check from the callers, which do it, into a signature, which cannot.

    Per job, not per file: a `timeout-minutes` on one job says nothing about the wedge budget
    of the job beside it, and a whole-file search would count it for both.

    `yaml.safe_load` rather than a line-anchored regex over the text. A regex that recognises a
    job by `^  name:$` cannot see `  wedgeable:  # added later` or `  "wedgeable":` — both
    ordinary YAML — and a job it cannot see is a job it reports as bounded. That failure is
    silent in both directions: it never lands in `missing`, and the job floor below only
    catches jobs that *disappear*, never ones that were never visible. `pyyaml` is a declared
    dependency and `tests/scripts/test_live_workflow_shape.py` already parses these same files
    with it, so this is one parser where the tree had two.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = document.get("jobs", {}) if isinstance(document, dict) else {}
    return jobs if isinstance(jobs, dict) else {}


def _workflow_text(name: str) -> str:
    text = (_WORKFLOWS / name).read_text(encoding="utf-8")
    assert text.strip(), f"{name} is empty, so every assertion against it would pass over nothing"
    return text


def _workflow_files() -> list[Path]:
    return sorted(path for glob in _WORKFLOW_GLOBS for path in _WORKFLOWS.glob(glob))


def test_no_workflow_installs_packages_with_a_bare_apt_get() -> None:
    """The command text has one home, and every call site reaches the bounded one."""
    offenders = {
        path.name: [
            line.strip()
            for line in _BARE_APT.findall(_YAML_COMMENT.sub("", path.read_text(encoding="utf-8")))
        ]
        for path in _workflow_files()
    }
    offenders = {name: lines for name, lines in offenders.items() if lines}

    # Non-empty first. "No workflow calls apt-get directly" is also true of a repo with no
    # workflows and of a glob that stopped matching, and both would make this test a no-op.
    invocations = {
        name: len(_SHARED_SCRIPT.findall(_workflow_text(name))) for name in _APT_WORKFLOWS
    }
    silent = sorted(name for name, count in invocations.items() if count == 0)
    assert not silent, (
        f"{silent} no longer invoke `./scripts/apt-install.sh`. If a workflow genuinely stopped "
        "installing system packages, drop it from _APT_WORKFLOWS; otherwise its apt step has "
        "escaped the timeout and retry and can wedge a job for the 360-minute default (#1978, "
        "ADR-0566)."
    )

    assert not offenders, (
        "a workflow calls `apt-get` directly instead of `./scripts/apt-install.sh`, so that step "
        "has no hard timeout and no retry: a blackholed mirror wedges the job until a human "
        f"cancels it, which is #1978. Offending lines: {offenders}. Route it through the shared "
        "script (`just apt-install <packages>` locally); pass KDIVE_APT_TIMEOUT_S if the package "
        "set is large enough to need a bigger budget, as live.yml does (ADR-0566)."
    )


def test_every_job_in_every_workflow_declares_a_timeout() -> None:
    """A hard timeout inside the script bounds one step; `timeout-minutes` bounds the rest.

    Every workflow, not only the package-installing ones (#1983). The 360-minute default is a
    property of GitHub Actions, not of `apt-get`: a job that wedges on a registry push or an
    emulated build burns the same six hours, and scoping this to `_APT_WORKFLOWS` left five
    workflows on the default with nothing to say so.

    **What this does not check, so a green run is not read as more than it is.** ADR-0566's
    convention has two halves — a value *sized from the job's observed runtime*, and that figure
    written in a comment beside it — and only the first is mechanised here, as presence and as a
    real bound. Nothing ties a declared number to any measurement: a job whose true cost is 15
    seconds can carry 300 with no comment and pass. Checking for a comment would only ever prove
    a `#` is present, never that its figure is current or true, so the provenance half of the
    convention is held by review and by ADR-0566, not by this test. `records.yml` is the one job
    already carrying a value with no observation written down.
    """
    paths = _workflow_files()
    # Non-empty first: "every job declares a timeout" is also true of a directory with no
    # workflows in it, and of a glob that stopped matching either spelling.
    assert paths, (
        f"no workflow files matched {_WORKFLOW_GLOBS} under {_WORKFLOWS.relative_to(_ROOT)}, so "
        "this guard is asserting nothing (ADR-0566, #1983)."
    )

    missing: list[str] = []
    unbounded: list[str] = []
    checked = 0
    for path in paths:
        # `_workflow_text` first only for its message: it names an *emptied* workflow as such,
        # where `assert jobs` one line down would report the same file as a layout change. Both
        # are loud; this one is right.
        _workflow_text(path.name)
        jobs = _jobs(path)
        assert jobs, f"no jobs parsed out of {path.name} — the workflow layout changed (ADR-0566)"
        for job_name, job in jobs.items():
            checked += 1
            uses = job.get("uses") if isinstance(job, dict) else None
            if uses is not None:
                # A job that calls a reusable workflow cannot declare `timeout-minutes` —
                # actionlint (`just lint-workflows`, on the same `just ci` chain as this test)
                # rejects it, and so does GitHub. Requiring it here would deadlock the first
                # such job against the repo's other gate. What makes the skip safe is that the
                # callee is a workflow this guard also reads, so assert that rather than
                # assuming it: an out-of-repo callee's jobs are invisible here and run on the
                # default, which is the one way this exemption becomes the hole it exists to
                # avoid. This is the guard's only skip, so it fails loudly instead of silently.
                assert str(uses).startswith("./"), (
                    f"{path.name}:{job_name} calls the out-of-repo reusable workflow {uses!r}. "
                    "Its jobs are not in this repo, so nothing here can check their "
                    "`timeout-minutes` and they run on the 360-minute Actions default, while "
                    "this guard reports the repo as bounded. A local callee (`./.github/...`) "
                    "is covered because this guard reads it directly (ADR-0566, #1983)."
                )
                continue
            declared = job.get("timeout-minutes") if isinstance(job, dict) else None
            if declared is None:
                missing.append(f"{path.name}:{job_name}")
            # `(int, float)` because GitHub's schema for this key is a float, so `0.5` is legal
            # and is a *tighter* bound than any integer. `not isinstance(declared, bool)` because
            # `bool` subclasses `int`: `timeout-minutes: true` parses to `True`, and `True >= 360`
            # is False — the one spelling that would slip past the check this assertion exists to
            # be. actionlint rejects it too, but not from this file.
            elif (
                not isinstance(declared, int | float)
                or isinstance(declared, bool)
                or declared >= _ACTIONS_DEFAULT_TIMEOUT_MINUTES
            ):
                unbounded.append(f"{path.name}:{job_name}={declared!r}")

    # The real count across the ten workflows is 17. `>= len(paths)` would be satisfied by a
    # parser that found one job per file and silently stopped checking the other seven. It is a
    # tripwire for that regression, not a coverage assertion, and it goes slack as jobs are
    # added — at 20 jobs a parser hiding three of them still clears 17. An exact count would
    # redden on every legitimate new job, which is churn this buys nothing for.
    assert checked >= 17, (
        f"only {checked} job(s) parsed across {[path.name for path in paths]}. Either the parser "
        "has drifted and this guard is checking a fraction of what it claims to, or a workflow "
        "or job was removed — if that removal was intended, lower this floor in the same change "
        "(ADR-0566)."
    )
    assert not missing, (
        f"{missing} declare no job-level `timeout-minutes`, so a wedged step there runs to the "
        "360-minute GitHub Actions default. That is what made #1978 cost a full CI cycle each "
        "time it fired. Size it from the job's observed runtime with headroom, and put the "
        "observed figure in a comment beside it. This rule covers every workflow, not only the "
        "package-installing ones ADR-0566 records — and where ADR-0566's sizing adds the apt "
        "script's ~11-minute worst case, a job with no apt step does not carry that term "
        "(ADR-0566, #1983)."
    )
    # Presence is not a bound. Without this, a job could satisfy the assertion above by
    # declaring exactly the default the assertion's own message names.
    assert not unbounded, (
        f"{unbounded} declare a `timeout-minutes` that is not a plain number below the "
        f"{_ACTIONS_DEFAULT_TIMEOUT_MINUTES}-minute Actions default. At the default it bounds "
        "nothing — the job is as wedgeable as it was before it was declared — and on a hosted "
        "runner nothing above it is enforceable either. Size it from the job's observed runtime "
        "(ADR-0566, #1983)."
    )


def test_apt_retry_shape_matches_the_prepull_script() -> None:
    """One retry contract across both network steps, not two that drift (ADR-0553, ADR-0566)."""
    shapes = {}
    for script in (_APT_SCRIPT, _PULL_SCRIPT):
        text = script.read_text(encoding="utf-8")
        backoff = _BACKOFF.search(text)
        assert backoff is not None, (
            f"{script.relative_to(_ROOT)} no longer declares `BACKOFF_S=( ... )`, so this guard "
            "can no longer see its retry shape (ADR-0566)."
        )
        assert _DERIVED_ATTEMPTS.search(text), (
            f"{script.relative_to(_ROOT)} no longer derives ATTEMPTS from BACKOFF_S. A second "
            "literal drifts from the array, and `set -u` aborts the moment it indexes past it."
        )
        shapes[script.name] = tuple(backoff.group("body").split())

    assert shapes[_APT_SCRIPT.name] == ("5", "15"), (
        f"the apt retry backoff is {shapes[_APT_SCRIPT.name]}, not the 5s/15s the issue's "
        "acceptance criteria and pull-test-images.sh both specify (#1978)."
    )
    assert shapes[_APT_SCRIPT.name] == shapes[_PULL_SCRIPT.name], (
        f"the two bounded-retry scripts have drifted apart: {shapes}. They implement one "
        "decision (ADR-0553) and a reader who checks either should not have to check both."
    )


def test_the_justfile_owns_the_command_text() -> None:
    """`AGENTS.md`: the justfile is the single source of truth for commands."""
    recipe = _JUST_RECIPE.search(_JUSTFILE.read_text(encoding="utf-8"))
    assert recipe is not None, (
        "the justfile no longer defines `apt-install +PACKAGES:`. The workflows invoke the "
        "script directly (they run before `just` is installed, and two of the jobs never "
        "install it at all), so this recipe is how a developer reaches the same command text "
        "(AGENTS.md, ADR-0566)."
    )
    assert _SHARED_SCRIPT.search(recipe.group("body")), (
        "the `apt-install` recipe no longer runs `./scripts/apt-install.sh`, so the recipe and "
        "the workflows have become two command strings instead of one (AGENTS.md)."
    )


def test_the_live_budget_override_reaches_the_script() -> None:
    """`live.yml` raises the install budget by env var; nothing else ties the two names."""
    live = _workflow_text("live.yml")
    assert re.search(r"^\s*KDIVE_APT_TIMEOUT_S:\s*\"\d+\"\s*$", live, re.MULTILINE), (
        "live.yml no longer sets KDIVE_APT_TIMEOUT_S. Its host-dep set is an order of magnitude "
        "larger than libvirt-dev, so on the default budget it runs on roughly 2x its measured "
        "install time (ADR-0566)."
    )
    assert "${KDIVE_APT_TIMEOUT_S" in _APT_SCRIPT.read_text(encoding="utf-8"), (
        "scripts/apt-install.sh no longer reads KDIVE_APT_TIMEOUT_S, so live.yml's override is "
        "inert and that job silently dropped to the default budget (ADR-0566)."
    )


# ---------------------------------------------------------------------------------------------
# Behavioral: run the script for real. `apt-get`, `sudo`, `dpkg` and `sleep` come from a stub
# directory prepended to PATH; `timeout` deliberately does not, because it is the thing on
# trial. Stubbing `sleep` is what keeps the 5s/15s backoff from being 20s of test runtime.
# ---------------------------------------------------------------------------------------------

_STUB_MIRROR = "stub-mirror.invalid"

#: Answers `apt_mirrors`' local config read; anything else is the scenario's behaviour.
_STUB_PREAMBLE = f"""#!/bin/sh
for arg in "$@"; do
  if [ "$arg" = "indextargets" ]; then
    printf 'Site: {_STUB_MIRROR}\\nRepo-URI: http://{_STUB_MIRROR}/ubuntu/\\n'
    exit 0
  fi
done
"""

#: The real `sleep`, resolved before the stub directory shadows it. The stub `sleep` below
#: exists to make the 5s/15s backoff instant, and a hang written as a bare `sleep 300` would
#: resolve to it and return at once — a stall test that never stalls, passing on any script.
_REAL_SLEEP = shutil.which("sleep")

#: `exec` so the outer `timeout` signals the sleeping process itself. Without it the stub's
#: shell dies on SIGTERM and orphans a `sleep` that keeps the captured stdout pipe open, and
#: the test hangs on a script that behaved correctly.
_STUB_HANGS = _STUB_PREAMBLE + f"exec {_REAL_SLEEP} 300\n"
_STUB_FAILS = _STUB_PREAMBLE + "echo 'E: Unable to fetch some archives' >&2\nexit 100\n"
_STUB_SUCCEEDS = _STUB_PREAMBLE + "exit 0\n"

#: The scenario the other three cannot reach. `_STUB_HANGS` and `_STUB_FAILS` both fail on the
#: *first* apt call, which is `update`, so the `install` call is never exercised on a failure
#: path — and every assertion about the bound is then satisfied by `update` alone. Measured:
#: without this stub, deleting `timeout` from the install call leaves all tests green, which is
#: #1978 itself shipping past its own guard.
_STUB_INSTALL_HANGS = (
    _STUB_PREAMBLE
    + f"""for arg in "$@"; do
  if [ "$arg" = "install" ]; then
    exec {_REAL_SLEEP} 300
  fi
done
exit 0
"""
)


#: The bound as one command, not as a bag of substrings. Membership tests on `"sudo env"`,
#: `"--kill-after"` and `"install"` separately are all satisfied by a script that bounds `update`
#: and leaves `install` bare — verified, that mutation passed every such assertion.
def _bounded_call(subcommand: str, budget: int) -> re.Pattern[str]:
    return re.compile(
        rf"sudo env DEBIAN_FRONTEND=noninteractive timeout --kill-after=10s {budget}s "
        rf"apt-get\b[^\n]*\b{subcommand}\b"
    )


_ATTEMPT_TIMEOUT_S = 1
#: Two apt calls per attempt, three attempts, plus interpreter and signal-delivery overhead.
#: Generous on purpose: this bounds a runaway, it does not measure performance.
_BOUND_S = 3 * 2 * _ATTEMPT_TIMEOUT_S + 20


def _stub_dir(tmp_path: Path, apt_get: str, argv_log: Path) -> Path:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # Every stub records the argv it was handed. Without this the stubs ignore their arguments
    # entirely, and the tests pass just as happily on a script that dropped `--kill-after`,
    # `sudo`, the `-o` options, or `--no-install-recommends`: nothing about the command apt is
    # actually given would be under test.
    record = f'printf "%s\\n" "$0 $*" >> {argv_log}\n'
    scripts = {
        "apt-get": apt_get,
        # `exec "$@"` and not a no-op: the script relies on sudo being transparent, and a sudo
        # that swallowed its argv would make every scenario below pass for the wrong reason.
        "sudo": f'#!/bin/sh\n{record}exec "$@"\n',
        "dpkg": f"#!/bin/sh\n{record}exit 0\n",
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, body in scripts.items():
        path = stubs / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return stubs


def _run(tmp_path: Path, apt_get: str) -> tuple[subprocess.CompletedProcess[str], float, str]:
    argv_log = tmp_path / "argv.log"
    env = dict(os.environ)
    env["PATH"] = f"{_stub_dir(tmp_path, apt_get, argv_log)}{os.pathsep}{env['PATH']}"
    env["KDIVE_APT_TIMEOUT_S"] = str(_ATTEMPT_TIMEOUT_S)
    started = time.monotonic()
    completed = subprocess.run(
        [str(_APT_SCRIPT), "libvirt-dev"],
        capture_output=True,
        text=True,
        env=env,
        # Well above _BOUND_S: if this fires, the script did not bound itself and the failure
        # should read as that rather than as a timeout the test imposed.
        timeout=_BOUND_S * 3,
        check=False,
    )
    recorded = argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""
    return completed, time.monotonic() - started, recorded


def test_a_stalled_apt_get_fails_the_step_within_the_budget(tmp_path: Path) -> None:
    """The claim in one test: a hang becomes a bounded, red, diagnosable failure (#1978)."""
    completed, elapsed, argv = _run(tmp_path, _STUB_HANGS)

    assert completed.returncode == 1, (
        "a permanently stalled apt-get must fail the step. Exhausting the attempt budget is a "
        f"failure, never a pass. Got exit {completed.returncode}.\n{completed.stderr}"
    )
    assert elapsed < _BOUND_S, (
        f"the script took {elapsed:.1f}s against a {_ATTEMPT_TIMEOUT_S}s per-call budget "
        f"(bound {_BOUND_S}s). The hard timeout is the half of #1978 that does the work — "
        "without it the step runs to the 360-minute Actions default."
    )
    for attempt in (1, 2, 3):
        assert f"attempt {attempt}/3" in completed.stderr, (
            f"attempt {attempt} of 3 is not named in the log, so a reader cannot tell a "
            f"first-try failure from an exhausted budget.\n{completed.stderr}"
        )
    assert "apt-get update stalled and was killed" in completed.stderr, (
        "a stall must be reported as a stall, naming the apt call that hung — not as an "
        "ordinary non-zero exit. The two have different causes and different fixes, and the "
        f"stall is the one #1978 is about.\n{completed.stderr}"
    )
    assert f"{_ATTEMPT_TIMEOUT_S}s install" in completed.stderr, (
        "the failure does not report the budget it was measured against, so a reader cannot "
        f"tell whether the budget or the mirror is what needs changing.\n{completed.stderr}"
    )
    assert _STUB_MIRROR in completed.stderr, (
        "the failure names no mirror, so the wedge is undiagnosable from the log, which is the "
        f"third acceptance criterion of #1978.\n{completed.stderr}"
    )
    # The `update` call, matched as one command. Each option below was individually deletable
    # without reddening any test before the stubs started recording argv.
    assert _bounded_call("update", _ATTEMPT_TIMEOUT_S).search(argv), (
        "`apt-get update` was not invoked as one bounded command — the budget, the SIGKILL grace "
        f"and the privilege drop have to hold together.\nRecorded argv:\n{argv}"
    )
    for required in (
        # `timeout` signals its own process group, and apt's default pty mode puts dpkg in a new
        # group and session where the kill cannot reach it — leaving an orphaned root dpkg on
        # the lock and failing every retry. Verified in ubuntu:24.04.
        "-o DPkg::Use-Pty=0",
        # Without this a retry racing a still-exiting dpkg dies instantly on the lock: apt's
        # compiled default is 0, meaning fail immediately.
        "-o DPkg::Lock::Timeout=30",
        "-o Acquire::Retries=0",
        # Secondary to the outer timeout, and deliberately asserted anyway: they are what makes a
        # blackholed mirror fail *inside* apt, where the error names the host and IP. A stub
        # apt-get cannot exercise a transport timeout, so argv is the only place to hold them.
        "-o Acquire::http::Timeout=15",
        "-o Acquire::https::Timeout=15",
        # A conffile prompt is a stall the retry cannot absorb — it is not transient.
        "-o Dpkg::Options::=--force-confold",
        "DEBIAN_FRONTEND=noninteractive",
    ):
        assert required in argv, (
            f"apt was never invoked with `{required}`, so the guarantee it carries is not in "
            f"force however the script reads.\nRecorded argv:\n{argv}"
        )
    assert "dpkg --configure -a" not in argv, (
        "the dpkg repair ran after a failed `update`, which unpacked nothing. Claiming a broken "
        f"package database there is a false diagnosis.\nRecorded argv:\n{argv}"
    )
    assert "::error::" in completed.stderr, (
        f"the final failure is not annotated for the Actions log.\n{completed.stderr}"
    )
    # The declared BACKOFF_S is checked statically above; this is the proof it is the array the
    # loop actually indexes, in order, rather than a constant nothing reads.
    for delay in (5, 15):
        assert f"retrying in {delay}s" in completed.stderr, (
            f"the {delay}s backoff step never ran, so BACKOFF_S is declared but not used as "
            f"written (#1978 acceptance criterion 2).\n{completed.stderr}"
        )


def test_a_failing_apt_get_is_retried_then_fails(tmp_path: Path) -> None:
    """A mirror that answers with an error is retried on the same budget, and still fails."""
    completed, _, _argv = _run(tmp_path, _STUB_FAILS)

    assert completed.returncode == 1, (
        f"exhausted attempts must fail the step, never pass it. Got {completed.returncode}."
    )
    assert "attempt 3/3: apt-get update exited 100" in completed.stderr, (
        "a non-zero exit must be reported with its status and distinguished from a stall.\n"
        f"{completed.stderr}"
    )
    assert "stalled" not in completed.stderr, (
        f"a clean non-zero exit was misreported as a stall.\n{completed.stderr}"
    )


def test_a_healthy_apt_get_succeeds_on_the_first_attempt(tmp_path: Path) -> None:
    """The counterweight: the tests above would also pass on a script that always fails."""
    completed, _, argv = _run(tmp_path, _STUB_SUCCEEDS)

    assert completed.returncode == 0, (
        f"a working apt-get must install and exit 0.\n{completed.stderr}"
    )
    assert "libvirt-dev" in completed.stdout, (
        f"the success line does not name what was installed.\n{completed.stdout}"
    )
    assert "attempt" not in completed.stderr, (
        f"a first-attempt success must not log a retry.\n{completed.stderr}"
    )
    # The install phase only runs when update succeeded, so this is the scenario that can see
    # the install flags at all.
    for required in ("apt-get", "install", "-y", "--no-install-recommends", "libvirt-dev"):
        assert required in argv, (
            f"the install was not invoked with `{required}`.\nRecorded argv:\n{argv}"
        )
    assert "dpkg --configure -a" not in argv, (
        "a successful install ran the dpkg repair, which should only follow a failed attempt — "
        f"it runs maintainer scripts and is not free.\nRecorded argv:\n{argv}"
    )


def test_a_stalled_install_is_bounded_and_repaired(tmp_path: Path) -> None:
    """The scenario the other stubs cannot reach: `update` succeeds and `install` hangs.

    `apt-get install` is the call named in #1978, and it only runs when `update` returned 0. A
    suite whose failure stubs both fail on the first call proves the bound for `update` and
    nothing else — measured: deleting `timeout` from the install call left all other tests green.
    """
    completed, elapsed, argv = _run(tmp_path, _STUB_INSTALL_HANGS)

    assert completed.returncode == 1, (
        f"a stalled install must fail the step, got exit {completed.returncode}.\n"
        f"{completed.stderr}"
    )
    assert elapsed < _BOUND_S, (
        f"the install call ran {elapsed:.1f}s against a {_ATTEMPT_TIMEOUT_S}s budget "
        f"(bound {_BOUND_S}s) — it is not bounded, which is #1978 unfixed for the very call the "
        "issue names."
    )
    assert _bounded_call("install", _ATTEMPT_TIMEOUT_S).search(argv), (
        "`apt-get install` was not invoked as one bounded command. A script that bounds `update` "
        "and leaves `install` bare satisfies every separate substring check and still wedges.\n"
        f"Recorded argv:\n{argv}"
    )
    assert "apt-get install stalled and was killed" in completed.stderr, (
        f"the stalled call is not named as the install phase.\n{completed.stderr}"
    )
    assert "may be local unpack work" in completed.stderr, (
        "an install-phase stall must not be reported as a network failure outright — it may be "
        f"local unpack work overrunning the budget.\n{completed.stderr}"
    )
    # Counted on the *bounded* invocation, not on the words appearing anywhere: both the `sudo`
    # and the `dpkg` stub record the same call, so a bare substring count double-counts and an
    # unbounded repair would still match. Once per failed attempt, including the last — skipping
    # it on exhaustion leaves a half-unpacked database with nothing in the log about it.
    repairs = re.findall(r"sudo timeout --kill-after=10s \d+s dpkg --configure -a", argv)
    assert len(repairs) == 3, (
        f"the bounded dpkg repair ran {len(repairs)} time(s), not once per failed attempt. It "
        "runs maintainer scripts and can block on a lock, so it has to be both present and "
        f"bounded.\nRecorded argv:\n{argv}"
    )


def test_a_zero_or_malformed_budget_is_refused(tmp_path: Path) -> None:
    """`timeout 0s` means *no limit*, so an unvalidated budget silently restores #1978."""
    # The stub PATH is needed even here: without an `apt-get` on PATH the script fails its host
    # preflight first, and this test would pass on exit 2 for the wrong reason.
    stubs = _stub_dir(tmp_path, _STUB_SUCCEEDS, tmp_path / "argv.log")
    # An *empty* value is deliberately not here: `${KDIVE_APT_TIMEOUT_S:-60}` treats it as unset
    # and falls back to the bounded default, which is the safe direction. Only a value that
    # parses as a budget but is not one needs refusing.
    for bad in ("0", "-5", "60s", "abc", "1e3", " 60"):
        completed = subprocess.run(
            [str(_APT_SCRIPT), "libvirt-dev"],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
                "KDIVE_APT_TIMEOUT_S": bad,
            },
            timeout=60,
            check=False,
        )
        assert completed.returncode == 2, (
            f"KDIVE_APT_TIMEOUT_S={bad!r} was not refused (exit {completed.returncode}). `0` in "
            "particular means no limit to GNU timeout, so accepting it would leave the script "
            "unbounded while the log still printed a budget.\n"
            f"{completed.stdout}{completed.stderr}"
        )
        assert "KDIVE_APT_TIMEOUT_S" in completed.stderr, (
            f"the rejection does not name the variable that is wrong.\n{completed.stderr}"
        )
