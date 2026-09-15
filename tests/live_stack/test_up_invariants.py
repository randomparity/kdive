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
    """The `--wait` set and the full backend set are declared separately, and stay separate.

    `docker compose up --wait` treats any container exit as a wait failure, so folding the
    run-to-completion seaweedfs-init back into the wait set makes a healthy stack report failure.
    """
    text = _LIB.read_text()
    assert "KDIVE_BACKEND_LONG_RUNNING=(postgres seaweedfs oidc)" in text
    assert "KDIVE_BACKEND_SERVICES=(postgres seaweedfs seaweedfs-init oidc)" in text
