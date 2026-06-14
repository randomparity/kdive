"""Tests for the distro → virt-builder base-template resolver (the build-fs extensibility seam)."""

from __future__ import annotations

import pytest

from kdive.images.distros import SUPPORTED_DISTROS, resolve_base_template


def test_fedora_resolves_to_virt_builder_template() -> None:
    assert resolve_base_template("fedora", "43") == "fedora-43"
    assert resolve_base_template("fedora", "42") == "fedora-42"


def test_only_fedora_is_supported_for_now() -> None:
    assert SUPPORTED_DISTROS == ("fedora",)


@pytest.mark.parametrize("distro", ["rocky", "debian", "bare", "ubuntu"])
def test_unimplemented_distro_raises_not_implemented_naming_the_distro(distro: str) -> None:
    with pytest.raises(NotImplementedError) as caught:
        resolve_base_template(distro, "43")
    message = str(caught.value)
    assert distro in message
    assert "fedora" in message, "the message names the supported distros as the fix"
