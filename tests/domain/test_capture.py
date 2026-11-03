"""Tests for the capture-method vocabulary (`kdive.domain.capture`)."""

from __future__ import annotations

import pytest

from kdive.domain.capture import CaptureMethod, capture_methods_for_arch
from kdive.domain.errors import CategorizedError, ErrorCategory

_LOCAL_METHODS = frozenset({CaptureMethod.KDUMP, CaptureMethod.FADUMP, CaptureMethod.HOST_DUMP})


def test_vocabulary_has_the_expected_methods() -> None:
    # fadump (ADR-0349) is the pseries firmware-assisted variant of the kdump capture family.
    assert {m.value for m in CaptureMethod} == {
        "console",
        "host_dump",
        "gdbstub",
        "kdump",
        "fadump",
    }


def test_x86_64_drops_only_fadump() -> None:
    # ArchTraits.fadump_capture is false on x86_64 (#3153, ADR-0757).
    assert capture_methods_for_arch(_LOCAL_METHODS, "x86_64") == frozenset(
        {CaptureMethod.KDUMP, CaptureMethod.HOST_DUMP}
    )


def test_ppc64le_keeps_the_provider_set() -> None:
    assert capture_methods_for_arch(_LOCAL_METHODS, "ppc64le") == _LOCAL_METHODS


@pytest.mark.parametrize("arch", ["x86_64", "ppc64le"])
def test_never_adds_a_method_the_provider_lacks(arch: str) -> None:
    assert capture_methods_for_arch(frozenset({CaptureMethod.HOST_DUMP}), arch) == frozenset(
        {CaptureMethod.HOST_DUMP}
    )
    assert capture_methods_for_arch(frozenset(), arch) == frozenset()


def test_unknown_arch_is_a_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        capture_methods_for_arch(_LOCAL_METHODS, "aarch64")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
