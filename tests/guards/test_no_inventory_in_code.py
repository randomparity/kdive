"""Guard: image/inventory definitions must live in ``systems.toml``, not in code (ADR-0112).

Phase 1 (#392) removes every in-code image definition — the ``images/seed_data`` rootfs YAML
tree, the inline rootfs/manifest YAML in ``admin/default_fixtures.py``, and the
``REMOTE_BASE_IMAGE_NAME`` literal — so the catalog is sourced only from the reconciled
``systems.toml`` ``[[image]]`` entries. This test pins those deletions so the definitions cannot
silently return to code.

The ``KDIVE_REMOTE_LIBVIRT_*`` singleton env-var assertion is intentionally NOT here: removing
those singletons is Phase 3 (#395). That guard is added by #395, not this issue.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "kdive"


def test_no_seed_data_tree() -> None:
    assert not (SRC / "images" / "seed_data").exists()


def test_no_inline_rootfs_yaml_in_fixtures() -> None:
    text = (SRC / "admin" / "default_fixtures.py").read_text(encoding="utf-8")
    assert "rootfs/fedora-kdive-ready" not in text
    assert "schema_version: 1" not in text  # the embedded manifest YAML


def test_no_remote_base_image_literal() -> None:
    text = (SRC / "providers" / "remote_libvirt" / "rootfs_build.py").read_text(encoding="utf-8")
    assert "fedora-kdive-remote-base-43" not in text
    assert "REMOTE_BASE_IMAGE_NAME" not in text
