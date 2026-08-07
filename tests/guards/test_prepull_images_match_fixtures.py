"""Guard: the CI pre-pull images match the tags the test fixtures actually start (ADR-0553).

`tests/db/conftest.py` and `tests/store/conftest.py` start their testcontainers lazily, from
inside the suite. When Docker Hub was unreachable that turned one dead registry into
`3 failed, 8938 passed, 16 skipped, 3448 errors` (#1913) — the cause was several screens down.

So CI pulls both images in a dedicated step before `just test`, and a registry outage fails
that step by itself. That step only means anything if it pulls the *same* tags the fixtures go
on to request: a pre-pull of a stale tag warms nothing, still passes, and leaves the cascade
exactly as it was — a guard-shaped no-op, which is worse than no step at all.

The tags are therefore duplicated (script + two conftests) and this guard owns the
duplication. It is deliberately **read-only** over all four files: it parses text and never
imports the fixtures, so it needs no Docker, no network, and no running daemon. It parses the
constants *by name* rather than by line number, so it survives edits around them.

Stdlib + pytest only, matching `test_workflow_action_pins.py`: this reads the tree, not the
project.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PULL_SCRIPT = _ROOT / "scripts" / "pull-test-images.sh"
_WORKFLOWS = _ROOT / ".github" / "workflows"

#: Every workflow that runs the suite against real containers must pre-pull first.
_SUITE_WORKFLOWS = ("ci.yml", "test-ordering.yml")

#: ``_POSTGRES_IMAGE = "postgres:17"`` — the fixture constant, matched by name, not by line.
_FIXTURE_CONSTANTS = {
    "tests/db/conftest.py": "_POSTGRES_IMAGE",
    "tests/store/conftest.py": "_MINIO_IMAGE",
}

#: The ``readonly IMAGES=( ... )`` array in the pull script, up to its closing paren.
_IMAGES_ARRAY = re.compile(r"^(?:readonly\s+)?IMAGES=\((?P<body>.*?)^\)", re.MULTILINE | re.DOTALL)
_QUOTED = re.compile(r'"(?P<image>[^"\s]+)"')

#: ``run: just pull-test-images`` and ``run: just test`` as whole steps. Anchored at the line
#: end so ``just test-compose-volumes`` and ``just test-ansible`` are not mistaken for the suite.
_PREPULL_STEP = re.compile(r"^\s*run:\s*just pull-test-images\s*$", re.MULTILINE)
_TEST_STEP = re.compile(r"^\s*run:\s*just test\s*$", re.MULTILINE)


def _fixture_image(rel_path: str, constant: str) -> str:
    text = (_ROOT / rel_path).read_text(encoding="utf-8")
    match = re.search(rf'^{re.escape(constant)}\s*=\s*"(?P<image>[^"]+)"', text, re.MULTILINE)
    assert match is not None, (
        f'{rel_path} no longer defines a module-level `{constant} = "..."`. The fixture image '
        "moved or was renamed; re-point this guard at its new home rather than deleting it — "
        "without it the CI pre-pull step can drift to a tag the suite never requests (ADR-0553)."
    )
    return match.group("image")


def _script_images() -> list[str]:
    text = _PULL_SCRIPT.read_text(encoding="utf-8")
    array = _IMAGES_ARRAY.search(text)
    assert array is not None, (
        f"{_PULL_SCRIPT.relative_to(_ROOT)} no longer declares an `IMAGES=( ... )` array, so this "
        "guard can no longer see what CI pre-pulls (ADR-0553)."
    )
    return [match.group("image") for match in _QUOTED.finditer(array.group("body"))]


def test_prepull_images_are_exactly_the_fixture_images() -> None:
    fixture_images = {
        _fixture_image(path, constant) for path, constant in _FIXTURE_CONSTANTS.items()
    }
    script_images = _script_images()

    # Non-empty first: two empty sets compare equal, which would make every assertion here
    # pass over nothing the moment a regex stopped matching.
    assert len(fixture_images) == len(_FIXTURE_CONSTANTS), (
        f"expected {len(_FIXTURE_CONSTANTS)} distinct fixture images, parsed {fixture_images}"
    )
    assert script_images, f"no images parsed from {_PULL_SCRIPT.relative_to(_ROOT)}"
    assert len(set(script_images)) == len(script_images), (
        f"{_PULL_SCRIPT.relative_to(_ROOT)} lists an image twice: {script_images}"
    )
    assert set(script_images) == fixture_images, (
        "the images CI pre-pulls and the images the test fixtures start have drifted, so the "
        "pre-pull step warms a tag the suite never requests and the registry cascade it exists "
        "to prevent is live again (ADR-0553, #1913). "
        f"Pre-pulled only: {sorted(set(script_images) - fixture_images)}; "
        f"started but never pre-pulled: {sorted(fixture_images - set(script_images))}. "
        f"Fixture constants: {_FIXTURE_CONSTANTS}"
    )


def test_every_suite_workflow_prepulls_before_it_runs_the_suite() -> None:
    # Every `just test`, not merely the first. `search` would let a second suite-running job
    # added later inherit the first job's pre-pull and pass while pre-pulling nothing for
    # itself — the guard going quiet exactly as the workflow grows.
    for name in _SUITE_WORKFLOWS:
        text = (_WORKFLOWS / name).read_text(encoding="utf-8")
        test_steps = [match.start() for match in _TEST_STEP.finditer(text)]
        prepull_steps = [match.start() for match in _PREPULL_STEP.finditer(text)]

        assert test_steps, (
            f"{name} no longer has a `run: just test` step, so this guard is vacuous for it. "
            "If the suite moved to another recipe, re-point _TEST_STEP; if the workflow stopped "
            "running the suite, drop it from _SUITE_WORKFLOWS (ADR-0553)."
        )
        assert len(prepull_steps) == len(test_steps), (
            f"{name} has {len(test_steps)} `run: just test` step(s) but "
            f"{len(prepull_steps)} `run: just pull-test-images` step(s). Each suite run needs "
            "its own pre-pull; without one, an unreachable registry surfaces as thousands of "
            "downstream fixture errors instead of one red step (ADR-0553, #1913)."
        )
        for index, (prepull, test) in enumerate(zip(prepull_steps, test_steps, strict=True)):
            assert prepull < test, (
                f"{name} runs `just pull-test-images` after `just test` (pair {index}), which "
                "pre-pulls nothing: the fixtures have already tried the registry by then "
                "(ADR-0553)."
            )
