"""Guard: CI's apt installs are bounded, retried, and written down exactly once (ADR-0566).

`Install libvirt build headers` wedged for 13 and 33 minutes on two runs in one afternoon
against a ~15s normal (#1978), and each time a human had to notice and cancel the job. The
failure is a **stall**, not a non-zero exit — `apt-get` never returned — so the load-bearing
half of the fix is the hard `timeout` in `scripts/apt-install.sh`, and the retry is what keeps a
timeout from being fatal on a merely slow mirror.

Three of these tests are static: they read the workflows, the script and the `justfile` and
assert the wiring. The last three actually **run** the script against a stub `apt-get` that
hangs, that fails, and that succeeds, because a wiring assertion cannot tell whether the timeout
fires. That is the whole claim, and asserting the absence of a bare `apt-get` would pass just as
happily over a script that hangs forever.

Stdlib + pytest only, matching `test_prepull_images_match_fixtures.py`: this reads the tree and
runs one shell script, not the project.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_APT_SCRIPT = _ROOT / "scripts" / "apt-install.sh"
_PULL_SCRIPT = _ROOT / "scripts" / "pull-test-images.sh"
_JUSTFILE = _ROOT / "justfile"
_WORKFLOWS = _ROOT / ".github" / "workflows"

#: Every workflow that installs system packages. Each must go through the shared script.
_APT_WORKFLOWS = ("ci.yml", "test-ordering.yml", "mcp-spec-drift.yml", "live.yml")

#: Every workflow whose jobs must declare a job-level `timeout-minutes`. Without one a wedged
#: step runs to the 360-minute Actions default, which is what made #1978 cost a whole CI cycle.
#: `live.yml` already sized both of its jobs; it is listed to keep that from being lost.
_TIMEOUT_WORKFLOWS = ("ci.yml", "test-ordering.yml", "mcp-spec-drift.yml", "live.yml")

#: An `apt-get` invocation as a command: start of line, optional `sudo`. A YAML comment line
#: starts with `#`, so mentioning apt-get in prose does not trip this.
_BARE_APT = re.compile(r"^[ \t]*(?:sudo[ \t]+)?apt-get\b.*$", re.MULTILINE)

#: The shared script, however a workflow spells the path to it.
_SHARED_SCRIPT = re.compile(r"\./scripts/apt-install\.sh\b")

#: `readonly BACKOFF_S=(5 15)` in either script.
_BACKOFF = re.compile(r"^(?:readonly\s+)?BACKOFF_S=\((?P<body>[^)]*)\)", re.MULTILINE)

#: `readonly ATTEMPTS=$((${#BACKOFF_S[@]} + 1))` — derived, never a second literal.
_DERIVED_ATTEMPTS = re.compile(
    r"^(?:readonly\s+)?ATTEMPTS=\$\(\(\$\{#BACKOFF_S\[@\]\}\s*\+\s*1\)\)", re.MULTILINE
)

#: A job key: two-space indent under the top-level `jobs:` mapping.
_JOBS_BLOCK = re.compile(r"^jobs:$", re.MULTILINE)
_JOB_KEY = re.compile(r"^  (?P<job>[A-Za-z0-9_-]+):$", re.MULTILINE)
_JOB_TIMEOUT = re.compile(r"^    timeout-minutes:\s*(?P<minutes>\d+)\s*$", re.MULTILINE)

#: `apt-install +PACKAGES:` and the body line that runs the script.
_JUST_RECIPE = re.compile(
    r"^apt-install\s+\+PACKAGES:\s*$\n(?P<body>(?:^[ \t]+.*$\n?)+)", re.MULTILINE
)


def _jobs(text: str) -> dict[str, str]:
    """Split a workflow into ``{job name: that job's YAML}``.

    Per job, not per file: a `timeout-minutes` on one job says nothing about the wedge budget
    of the job beside it, and a whole-file search would count it for both.
    """
    block = _JOBS_BLOCK.search(text)
    if block is None:
        return {}
    body = text[block.end() :]
    bounds = list(_JOB_KEY.finditer(body))
    ends = [*(match.start() for match in bounds[1:]), len(body)]
    return {
        match.group("job"): body[match.end() : end] for match, end in zip(bounds, ends, strict=True)
    }


def _workflow_text(name: str) -> str:
    text = (_WORKFLOWS / name).read_text(encoding="utf-8")
    assert text.strip(), f"{name} is empty, so every assertion against it would pass over nothing"
    return text


def test_no_workflow_installs_packages_with_a_bare_apt_get() -> None:
    """The command text has one home, and every call site reaches the bounded one."""
    offenders = {
        path.name: [line.strip() for line in _BARE_APT.findall(path.read_text(encoding="utf-8"))]
        for path in sorted(_WORKFLOWS.glob("*.yml"))
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


def test_every_job_in_a_package_installing_workflow_declares_a_timeout() -> None:
    """A hard timeout inside the script bounds one step; `timeout-minutes` bounds the rest."""
    missing: list[str] = []
    checked = 0
    for name in _TIMEOUT_WORKFLOWS:
        jobs = _jobs(_workflow_text(name))
        assert jobs, f"no jobs parsed out of {name} — the workflow layout changed (ADR-0566)"
        for job_name, body in jobs.items():
            checked += 1
            if not _JOB_TIMEOUT.search(body):
                missing.append(f"{name}:{job_name}")

    assert checked >= len(_TIMEOUT_WORKFLOWS), (
        f"only {checked} job(s) parsed across {list(_TIMEOUT_WORKFLOWS)}; the parser has drifted "
        "and this guard is checking almost nothing (ADR-0566)."
    )
    assert not missing, (
        f"{missing} declare no job-level `timeout-minutes`, so a wedged step there runs to the "
        "360-minute GitHub Actions default. That is what made #1978 cost a full CI cycle each "
        "time it fired. Size it from the job's observed runtime with headroom (ADR-0566)."
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

_ATTEMPT_TIMEOUT_S = 1
#: Two apt calls per attempt, three attempts, plus interpreter and signal-delivery overhead.
#: Generous on purpose: this bounds a runaway, it does not measure performance.
_BOUND_S = 3 * 2 * _ATTEMPT_TIMEOUT_S + 20


def _stub_dir(tmp_path: Path, apt_get: str) -> Path:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    scripts = {
        "apt-get": apt_get,
        # `exec "$@"` and not a no-op: the script relies on sudo being transparent, and a sudo
        # that swallowed its argv would make every scenario below pass for the wrong reason.
        "sudo": '#!/bin/sh\nexec "$@"\n',
        "dpkg": "#!/bin/sh\nexit 0\n",
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, body in scripts.items():
        path = stubs / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return stubs


def _run(tmp_path: Path, apt_get: str) -> tuple[subprocess.CompletedProcess[str], float]:
    env = dict(os.environ)
    env["PATH"] = f"{_stub_dir(tmp_path, apt_get)}{os.pathsep}{env['PATH']}"
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
    return completed, time.monotonic() - started


def test_a_stalled_apt_get_fails_the_step_within_the_budget(tmp_path: Path) -> None:
    """The claim in one test: a hang becomes a bounded, red, diagnosable failure (#1978)."""
    completed, elapsed = _run(tmp_path, _STUB_HANGS)

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
    completed, _ = _run(tmp_path, _STUB_FAILS)

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
    completed, _ = _run(tmp_path, _STUB_SUCCEEDS)

    assert completed.returncode == 0, (
        f"a working apt-get must install and exit 0.\n{completed.stderr}"
    )
    assert "libvirt-dev" in completed.stdout, (
        f"the success line does not name what was installed.\n{completed.stdout}"
    )
    assert "attempt" not in completed.stderr, (
        f"a first-attempt success must not log a retry.\n{completed.stderr}"
    )
