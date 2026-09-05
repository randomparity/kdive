"""Tests for the durable remote boot-artifact volume-name grammar (ADR-0599)."""

from uuid import UUID

import pytest

from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_name import (
    BOOT_ARTIFACT_NAME_MAX_BYTES,
    BootArtifactKind,
    BootArtifactName,
    parse_boot_artifact_name,
    render_boot_artifact_name,
)

SYSTEM = UUID("8f1c6d1e-2b4a-4c3d-9e0f-1a2b3c4d5e6f")
RUN = UUID("0a1b2c3d-4e5f-4061-8273-849506a7b8c9")
ATTEMPT = UUID("10203040-5060-4070-8090-a0b0c0d0e0f0")
DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize("kind", ["kernel", "initrd"])
def test_final_name_round_trip_recovers_complete_identity(kind: BootArtifactKind) -> None:
    name = render_boot_artifact_name(kind, SYSTEM, RUN, DIGEST)

    assert name == f"kdive-boot-v1-{kind}-{SYSTEM}-{RUN}-{'a' * 64}-final"
    assert parse_boot_artifact_name(name) == BootArtifactName(
        name=name,
        kind=kind,
        system_id=SYSTEM,
        run_id=RUN,
        digest=DIGEST,
        partial=False,
        attempt_id=None,
    )


@pytest.mark.parametrize("kind", ["kernel", "initrd"])
def test_partial_name_round_trip_recovers_attempt(kind: BootArtifactKind) -> None:
    name = render_boot_artifact_name(kind, SYSTEM, RUN, DIGEST, attempt_id=ATTEMPT)

    assert name.endswith(f"-partial-{ATTEMPT}")
    parsed = parse_boot_artifact_name(name)
    assert parsed is not None
    assert parsed.owner == (kind, SYSTEM, RUN, DIGEST)
    assert parsed.partial
    assert parsed.attempt_id == ATTEMPT


def test_longest_name_is_204_ascii_bytes_within_the_dir_pool_limit() -> None:
    longest = render_boot_artifact_name("initrd", SYSTEM, RUN, DIGEST, attempt_id=ATTEMPT)

    assert len(longest.encode("ascii")) == 204
    assert len(longest.encode("ascii")) < BOOT_ARTIFACT_NAME_MAX_BYTES


@pytest.mark.parametrize(
    "name",
    [
        "",
        f"kdive-boot-v2-kernel-{SYSTEM}-{RUN}-{'a' * 64}-final",
        f"kdive-boot-v1-other-{SYSTEM}-{RUN}-{'a' * 64}-final",
        f"kdive-boot-v1-kernel-{str(SYSTEM).upper()}-{RUN}-{'a' * 64}-final",
        f"kdive-boot-v1-kernel-{SYSTEM}-{RUN}-{'A' * 64}-final",
        f"kdive-boot-v1-kernel-{SYSTEM}-{RUN}-{'a' * 63}-final",
        f"kdive-boot-v1-kernel-{SYSTEM}-{RUN}-{'a' * 65}-final",
        f"kdive-boot-v1-kernel-{SYSTEM}-{RUN}-{'a' * 64}-partial",
        f"kdive-boot-v1-kernel-{SYSTEM}-{RUN}-{'a' * 64}-final-{ATTEMPT}",
        f"kdive-boot-v1-kernel-{SYSTEM}-{RUN}-{'a' * 64}-partial-{ATTEMPT}-extra",
        f"x-kdive-boot-v1-kernel-{SYSTEM}-{RUN}-{'a' * 64}-final",
        f"kdive-boot-v1-kernel-{SYSTEM}-{RUN}-{'a' * 64}-final/../owned",
        "x" * 300,
        f"kdive-kernel-{SYSTEM}-{RUN}",
    ],
)
def test_foreign_or_malformed_name_does_not_parse(name: str) -> None:
    assert parse_boot_artifact_name(name) is None


@pytest.mark.parametrize("digest", ["", "a" * 64, "sha256:" + "A" * 64, "sha256:x"])
def test_renderer_rejects_malformed_digest(digest: str) -> None:
    with pytest.raises(ValueError, match="digest"):
        render_boot_artifact_name("kernel", SYSTEM, RUN, digest)
