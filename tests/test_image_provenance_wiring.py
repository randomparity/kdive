"""Structural guard for the container version-provenance wiring (ADR-0370).

The end-to-end proof — that a running image self-reports ``X.Y.Z[-dev]+g<sha>`` — lives in the
gated ``tests/image`` smoke suite, which needs Docker and a built image. This guard is the cheap
belt-and-suspenders that runs in the plain unit gate: it fails fast, without a build, if either
end of the wiring is deleted (the Dockerfile stamp step, the ``.dockerignore`` exclusion, or the
build-args in the two workflows that build the image). A regression there silently restores the
``X.Y.Z-dev`` (no commit) reporting the ADR fixes, so it must be machine-checked, not trusted.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_dockerfile_stamps_buildinfo_from_build_args() -> None:
    dockerfile = _read("Dockerfile")
    assert "ARG KDIVE_COMMIT" in dockerfile, "ADR-0370 wiring: Dockerfile lost ARG KDIVE_COMMIT"
    assert "ARG KDIVE_RELEASE" in dockerfile, "ADR-0370 wiring: Dockerfile lost ARG KDIVE_RELEASE"
    assert "stamp-buildinfo.sh" in dockerfile, (
        "ADR-0370 wiring: Dockerfile no longer stamps _buildinfo.py via stamp-buildinfo.sh"
    )
    assert 'KDIVE_BUILDINFO_COMMIT="$KDIVE_COMMIT"' in dockerfile, (
        "ADR-0370 wiring: Dockerfile stamp step does not pass the commit override through"
    )
    # The stamp must stay guarded by a non-empty KDIVE_COMMIT so a no-arg build (local/PR) does
    # not fail on the git-less builder stage.
    assert '-n "$KDIVE_COMMIT"' in dockerfile, (
        "ADR-0370 wiring: Dockerfile stamp step lost its KDIVE_COMMIT non-empty guard"
    )


def test_dockerignore_excludes_stale_buildinfo() -> None:
    dockerignore = _read(".dockerignore")
    assert "src/kdive/_buildinfo.py" in dockerignore, (
        "ADR-0370 wiring: .dockerignore no longer excludes a stale local _buildinfo.py"
    )


def test_workflows_pass_provenance_build_args() -> None:
    for workflow in (".github/workflows/release-image.yml", ".github/workflows/ci.yml"):
        text = _read(workflow)
        assert "KDIVE_COMMIT=" in text, f"ADR-0370 wiring: {workflow} stopped passing KDIVE_COMMIT"
        assert "KDIVE_RELEASE=" in text, (
            f"ADR-0370 wiring: {workflow} stopped passing KDIVE_RELEASE"
        )
        assert "rev-parse --short=12 HEAD" in text, (
            f"ADR-0370 wiring: {workflow} lost the pinned --short=12 provenance SHA"
        )
