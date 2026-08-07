"""The testcontainer pre-pull step: does it actually retry, and does it fail when it can't?

``tests/guards/test_prepull_images_match_fixtures.py`` reads this script as text — it checks
the tags cannot drift from the fixtures', and never runs it. So the retry itself had no
executing coverage: the `BACKOFF_S[attempt - 1]` index, and the derived `ATTEMPTS` that exists
to keep that index in bounds, would only be exercised by a CI run with a genuinely failing
pull. Under ``set -u`` an off-by-one there aborts the script between attempts with a bare
`unbound variable` (ADR-0553).

Driven here with a stub ``docker`` and a no-op ``sleep``, so it needs no daemon and no network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "pull-test-images.sh"


def _run(tmp_path: Path, *, pull_exit: int) -> tuple[int, list[str], list[str]]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    calls = tmp_path / "pulls.txt"
    sleeps = tmp_path / "sleeps.txt"
    calls.write_text("", encoding="utf-8")
    sleeps.write_text("", encoding="utf-8")

    (stub_dir / "docker").write_text(
        f'#!/usr/bin/env bash\necho "$*" >> "$STUB_PULLS"\nexit {pull_exit}\n', encoding="utf-8"
    )
    (stub_dir / "docker").chmod(0o755)
    (stub_dir / "sleep").write_text(
        '#!/usr/bin/env bash\necho "$1" >> "$STUB_SLEEPS"\nexit 0\n', encoding="utf-8"
    )
    (stub_dir / "sleep").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    env["STUB_PULLS"] = str(calls)
    env["STUB_SLEEPS"] = str(sleeps)

    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(_SCRIPT)], capture_output=True, text=True, check=False, cwd=_ROOT, env=env
    )
    return (
        completed.returncode,
        calls.read_text(encoding="utf-8").splitlines(),
        sleeps.read_text(encoding="utf-8").splitlines(),
    )


def test_a_reachable_registry_pulls_each_image_once_without_waiting(tmp_path: Path) -> None:
    exit_code, pulls, sleeps = _run(tmp_path, pull_exit=0)

    assert exit_code == 0
    assert len(pulls) == 2, f"expected one pull per image, got {pulls}"
    assert not sleeps, "a successful pull must not back off"


def test_an_unreachable_registry_exhausts_the_budget_then_fails(tmp_path: Path) -> None:
    exit_code, pulls, sleeps = _run(tmp_path, pull_exit=1)

    assert exit_code == 1, "a registry outage must fail this step, not fall through to the suite"
    # The script stops at the first image it cannot get, so exactly one image is retried.
    assert len(pulls) == 3, f"expected the full attempt budget for one image, got {pulls}"
    assert sleeps == ["5", "15"], f"expected the declared backoff between attempts, got {sleeps}"
