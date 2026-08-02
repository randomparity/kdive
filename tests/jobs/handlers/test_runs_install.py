"""Coverage anchor for the split install run handler module."""

from __future__ import annotations

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.handlers.runs import install as runs_install
from kdive.jobs.handlers.runs import registrar as runs


def test_install_handler_is_exported_through_runs_facade() -> None:
    assert runs.install_handler is runs_install.install_handler


def test_reusable_install_requires_every_referenced_artifact_version() -> None:
    refs = {"kernel": "k", "initrd": "i", "vmlinux": "v"}

    assert runs_install._validated_artifact_versions(None, refs, None) is None
    assert runs_install._validated_artifact_versions(
        "digest.generation", refs, {"kernel": "kv", "initrd": "iv", "vmlinux": "vv"}
    ) == {"kernel": "kv", "initrd": "iv", "vmlinux": "vv"}

    for versions in (None, {}, {"kernel": "kv"}, {"kernel": "kv", "initrd": ""}):
        try:
            runs_install._validated_artifact_versions("digest.generation", refs, versions)
        except CategorizedError as exc:
            assert exc.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            assert exc.details["reason"] == "reusable_build_versions_incomplete"
        else:
            raise AssertionError(f"incomplete versions unexpectedly accepted: {versions!r}")
