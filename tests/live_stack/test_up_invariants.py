"""Guard the invariants that keep bring-up from fighting the host app tier.

No bring-up file may start the kdive:dev compose app tier (migrate/server/worker/reconciler) —
the host processes own that tier and the host apply-migrations.sh is the authoritative
migrator. These are text-level guards because the scripts are not import-testable.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIVE_STACK = _REPO_ROOT / "scripts" / "live-stack"
_SERVICES = _LIVE_STACK / "stack-services.sh"
_LIB = _LIVE_STACK / "lib.sh"
# Both files, because the backend `compose up` lives in lib.sh while the app-tier reconcile and
# the observability profile stay in the script. The invariant is about bring-up, not one file.
_BRING_UP_FILES = (_SERVICES, _LIB)
_APP_TIER = ("migrate", "server", "worker", "reconciler")
# Any `compose ... up ...` invocation, regardless of intervening flags (e.g. `--profile obs`).
_COMPOSE_UP = re.compile(r"compose\b.*\bup\b")


def _compose_up_lines(text: str) -> list[str]:
    """Non-comment `compose ... up` lines.

    Comments are skipped because a comment can mention `compose ... up` without running
    anything — lib.sh has one naming KDIVE_WORKER_COUNT, which the case-insensitive check
    below would otherwise fail on. The observability line is deliberately NOT skipped, so a
    future `--profile obs up -d server` still fails this guard.
    """
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _COMPOSE_UP.search(line):
            lines.append(line)
    return lines


def test_up_reconciles_app_tier_before_start() -> None:
    """The reconcile exists. That it is REACHED on the default `services` path is proved
    behaviourally by tests/scripts/test_live_stack_scripts.py::
    test_services_stage_reconciles_the_app_tier — this substring check cannot see the stage
    gate the block now sits under."""
    text = _SERVICES.read_text()
    assert "rm -sf migrate server worker reconciler" in text


@pytest.mark.parametrize("path", _BRING_UP_FILES, ids=lambda p: p.name)
def test_bring_up_never_starts_the_app_tier(path: Path) -> None:
    # Match `compose up` even with flags between (`compose --profile obs up`); a naive
    # "compose up" substring check would miss the profile-flag form and let the very
    # regression this guard exists to catch slip through. Case-insensitively, because a guard
    # whose verdict depends on letter case is not a guard.
    for line in _compose_up_lines(path.read_text()):
        for svc in _APP_TIER:
            assert not re.search(rf"\b{svc}\b", line, re.IGNORECASE), (
                f"{path.name} starts app-tier service in: {line!r}"
            )


def test_wait_set_excludes_the_one_shot() -> None:
    """`--wait` is applied to the long-running subset, never to the full backend set.

    `docker compose up --wait` treats any container exit as a wait failure, so waiting on the
    run-to-completion seaweedfs-init makes a healthy stack report failure. Asserted against the
    `--wait` line itself rather than the array declarations: two correct declarations with the
    wrong one passed to `--wait` is exactly the regression, and a declaration check misses it.
    """
    text = _LIB.read_text()
    assert "KDIVE_BACKEND_LONG_RUNNING=(postgres seaweedfs oidc)" in text
    # The full set has one consumer left (stack-status.sh), so nothing else would notice a
    # "dead variable" cleanup. Under `set -u` an unset array expands to zero words rather than
    # erroring, so the status report would silently stop listing the backends and still exit 0.
    assert "KDIVE_BACKEND_SERVICES=(postgres seaweedfs seaweedfs-init oidc)" in text
    wait_lines = [ln for ln in _compose_up_lines(text) if "--wait" in ln]
    assert len(wait_lines) == 1, wait_lines
    # Pins the identifier, not the three names: the contract is that the declared long-running
    # array is what reaches `--wait`. Membership is pinned by the assertion above, and
    # test_backends_stage_waits_only_on_the_long_running_backends covers the same contract
    # through recorded argv, without the name coupling.
    assert "KDIVE_BACKEND_LONG_RUNNING[@]" in wait_lines[0], wait_lines[0]
    assert "KDIVE_BACKEND_SERVICES" not in wait_lines[0], wait_lines[0]
