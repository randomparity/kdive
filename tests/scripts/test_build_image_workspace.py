"""Contract tests for the local example's image-build workspace."""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "local-libvirt" / "build-image.sh"


def test_workspace_is_canonicalized_and_labeled_before_build_fs() -> None:
    """The label and build command use the one canonical workspace in order."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert (
        source.index('realpath -m -- "${workspace}"')
        < source.index('mkdir -p "${workspace}"')
        < source.index('kdive_label_svirt_image "${workspace}"')
        < source.index('build-fs --image "${name}" --workspace "${workspace}"')
    )
