import pytest

from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    MODULE_VOLUME_KINDS,
    MODULE_VOLUME_NAME_MAX_BYTES,
    ModuleVolumeOwner,
    parse_module_volume_name,
    render_module_volume_name,
)

SYSTEM_ID = "8f1c6d1e-2b4a-4c3d-9e0f-1a2b3c4d5e6f"
RUN_ID = "0a1b2c3d-4e5f-4061-8273-849506a7b8c9"
NONCE = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret - fixture operation nonce

VALID_NAME = f"kdive-module-{SYSTEM_ID}-{RUN_ID}-{NONCE}-source.ext4"


def test_grammar_covers_every_kind_the_provider_renders() -> None:
    # Pinned to literals, not to MODULE_VOLUME_KINDS: a kind dropped from the constant would
    # otherwise shrink the round-trip parametrisation along with the grammar, and ADR-0588 makes
    # an omitted kind a volume the sweep classifies as foreign and leaks forever.
    assert MODULE_VOLUME_KINDS == (
        "source.ext4",
        "scratch.ext4",
        "reaping.journal",
        "reaped.journal",
    )


@pytest.mark.parametrize(
    "kind", ["source.ext4", "scratch.ext4", "reaping.journal", "reaped.journal"]
)
def test_round_trip_recovers_the_owner(kind: str) -> None:
    name = render_module_volume_name(SYSTEM_ID, RUN_ID, NONCE, kind)
    assert name == f"kdive-module-{SYSTEM_ID}-{RUN_ID}-{NONCE}-{kind}"
    assert parse_module_volume_name(name) == ModuleVolumeOwner(
        system_id=SYSTEM_ID, run_id=RUN_ID, operation_nonce=NONCE, kind=kind
    )


def test_longest_name_fits_the_budget() -> None:
    longest = render_module_volume_name(
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "f" * 32,
        "reaping.journal",
    )
    per_kind = [render_module_volume_name(SYSTEM_ID, RUN_ID, NONCE, k) for k in MODULE_VOLUME_KINDS]
    assert max(len(name) for name in per_kind) == len(longest)
    assert len(longest.encode()) == 135
    assert len(longest.encode()) < MODULE_VOLUME_NAME_MAX_BYTES


@pytest.mark.parametrize(
    "name",
    [
        "",
        "kdive-module-",
        VALID_NAME.removesuffix("-source.ext4"),
        VALID_NAME.replace("source.ext4", "backup.ext4"),
        VALID_NAME.replace("source.ext4", "reaping.log"),
        VALID_NAME.replace(SYSTEM_ID, SYSTEM_ID.replace("8f", "8F")),
        VALID_NAME.replace(NONCE, NONCE[:31]),
        VALID_NAME.replace(NONCE, NONCE + "0"),
        "x" + VALID_NAME,
        VALID_NAME + "x",
        f"kdive-module-{SYSTEM_ID}-{RUN_ID}-{NONCE}/source.ext4",
        "../" + VALID_NAME,
        "x" * 300,
        "fedora-42-base.qcow2",
    ],
    ids=[
        "empty",
        "prefix-only",
        "kind-removed",
        "unknown-ext4-kind",
        "near-miss-journal-kind",
        "uppercase-uuid-digit",
        "short-nonce",
        "long-nonce",
        "leading-junk",
        "trailing-junk",
        "slash-separator",
        "traversal-prefix",
        "overlong",
        "operator-base-image",
    ],
)
def test_foreign_names_do_not_parse(name: str) -> None:
    assert parse_module_volume_name(name) is None


@pytest.mark.parametrize(
    ("system_id", "run_id", "nonce", "kind"),
    [
        ("not-a-uuid", RUN_ID, NONCE, "source.ext4"),
        (SYSTEM_ID, "not-a-uuid", NONCE, "source.ext4"),
        (SYSTEM_ID, RUN_ID, NONCE.upper(), "source.ext4"),
        (SYSTEM_ID, RUN_ID, NONCE, "backup.ext4"),
    ],
    ids=["non-uuid-system", "non-uuid-run", "uppercase-nonce", "unknown-kind"],
)
def test_render_rejects_malformed_input(system_id: str, run_id: str, nonce: str, kind: str) -> None:
    with pytest.raises(ValueError, match="module volume"):
        render_module_volume_name(system_id, run_id, nonce, kind)
