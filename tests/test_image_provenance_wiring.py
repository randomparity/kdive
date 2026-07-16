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
        assert "rev-parse --short=12 HEAD" in text, (
            f"ADR-0370 wiring: {workflow} lost the pinned --short=12 provenance SHA"
        )


def test_release_workflow_marks_tag_builds_as_release() -> None:
    # The whole point is that a v* tag image reports X.Y.Z+g<sha> (release) and :edge reports
    # X.Y.Z-dev+g<sha>. Assert the *rendering signal*, not just that the token exists: a bare
    # `KDIVE_RELEASE=false` in the release workflow would pass a token-presence check yet ship a
    # tag image mislabeled -dev, and the multi-arch release job has no runtime --version gate.
    release = _read(".github/workflows/release-image.yml")
    assert "KDIVE_RELEASE=${{ startsWith(github.ref, 'refs/tags/v') }}" in release, (
        "ADR-0370 wiring: release-image.yml must derive KDIVE_RELEASE from the v* tag ref, not a "
        "literal — otherwise a released tag image self-reports X.Y.Z-dev"
    )
    # A v* tag bakes RELEASE=true, so the tag must equal the pyproject version or the image's
    # baked X.Y.Z disagrees with its :X.Y.Z registry tag. release.yml enforces this for the wheel;
    # the release image path must too, since it has no runtime --version gate.
    assert "Verify tag matches pyproject version" in release, (
        "ADR-0370 wiring: release-image.yml must verify the v* tag equals the pyproject version "
        "before baking RELEASE=true"
    )


def test_ci_pr_build_is_never_release() -> None:
    # A PR build is never a release, so ci.yml pins KDIVE_RELEASE=false; this is what lets the
    # image-smoke test assert the -dev shape deterministically.
    ci = _read(".github/workflows/ci.yml")
    assert "KDIVE_RELEASE=false" in ci, (
        "ADR-0370 wiring: ci.yml must pin KDIVE_RELEASE=false for the non-release PR image"
    )
