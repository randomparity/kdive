"""Guard the invariants that keep up.sh from fighting the host app tier.

up.sh must never start the kdive:dev compose app tier (migrate/server/worker/reconciler) —
the host processes own that tier and the host apply-migrations.sh is the authoritative
migrator. These are text-level guards because the scripts are not import-testable.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UP = _REPO_ROOT / "scripts" / "live-stack" / "up.sh"
_APP_TIER = ("migrate", "server", "worker", "reconciler")
# Any `compose ... up ...` invocation, regardless of intervening flags (e.g. `--profile obs`).
_COMPOSE_UP = re.compile(r"compose\b.*\bup\b")


def test_up_reconciles_app_tier_before_start() -> None:
    text = _UP.read_text()
    assert "rm -sf migrate server worker reconciler" in text


def test_up_never_starts_the_app_tier() -> None:
    text = _UP.read_text()
    for line in text.splitlines():
        # Match `compose up` even with flags between (`compose --profile obs up`); a naive
        # "compose up" substring check would miss the profile-flag form and let the very
        # regression this guard exists to catch slip through.
        if not _COMPOSE_UP.search(line):
            continue
        for svc in _APP_TIER:
            assert not re.search(rf"\b{svc}\b", line), f"up.sh starts app-tier service in: {line!r}"


def test_up_uses_the_canonical_backend_list() -> None:
    text = _UP.read_text()
    assert "KDIVE_BACKEND_SERVICES" in text
