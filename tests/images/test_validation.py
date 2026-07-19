"""Guest-contract validation of a built/uploaded rootfs image (ADR-0092/0093, issue #285).

``validate_guest_contract`` libguestfs-inspects the image and raises a
``CategorizedError(CONFIGURATION_ERROR)`` naming the first missing contract element. The slow
libguestfs probe is an injected seam so these tests run without libguestfs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from kdive.domain.catalog.images import Capability
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.cataloging import validation
from kdive.images.cataloging.validation import (
    DEFAULT_INSPECT,
    GUEST_CONTRACT_PATHS,
    InspectSeam,
    validate_guest_contract,
)


def test_guest_contract_elements_are_a_subset_of_the_capability_vocabulary() -> None:
    # The upload path stores validated `required` guest-contract elements as image capabilities,
    # so every guest-contract key must be a Capability (ADR-0286); the vocabularies cannot drift.
    assert set(GUEST_CONTRACT_PATHS) <= {c.value for c in Capability}


def _present(*paths: str) -> InspectSeam:
    """An inspection seam reporting exactly ``paths`` as present in the image."""

    def _probe(qcow2_path: Path, candidates: Sequence[str]) -> set[str]:
        return {p for p in candidates if p in paths}

    return _probe


def test_passes_when_every_required_element_is_present(tmp_path: Path) -> None:
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")
    required = ["kdump", "drgn"]
    present = _present(*[GUEST_CONTRACT_PATHS[r] for r in required])
    # Does not raise.
    validate_guest_contract(image, required=required, inspect=present)


def test_names_the_missing_element(tmp_path: Path) -> None:
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")
    # kdump is present; drgn is absent.
    present = _present(GUEST_CONTRACT_PATHS["kdump"])

    with pytest.raises(CategorizedError) as err:
        validate_guest_contract(image, required=["drgn", "kdump"], inspect=present)

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
    # The error names the missing element (the first one), not a generic failure.
    assert "drgn" in str(err.value)
    assert err.value.details == {"missing": "drgn", "path": GUEST_CONTRACT_PATHS["drgn"]}


def test_probe_receives_the_image_path(tmp_path: Path) -> None:
    # The inspection seam is handed the image under test, not some other path.
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")
    seen: list[Path] = []

    def _probe(qcow2_path: Path, candidates: Sequence[str]) -> set[str]:
        seen.append(qcow2_path)
        return set(candidates)

    validate_guest_contract(image, required=["drgn"], inspect=_probe)
    assert seen == [image]


@pytest.mark.parametrize("retired", ["agent", "helpers"])
def test_retired_contract_elements_are_now_unknown(tmp_path: Path, retired: str) -> None:
    # `agent` (qemu-ga) and `helpers` named guest-contract markers no local family bakes; they
    # were dropped from the vocabulary, so requiring one is now a configuration error, not a
    # silent pass against a phantom path.
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")
    with pytest.raises(CategorizedError) as err:
        validate_guest_contract(image, required=[retired], inspect=_present())
    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert retired in str(err.value)


def test_unknown_required_element_is_a_configuration_error(tmp_path: Path) -> None:
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")

    with pytest.raises(CategorizedError) as err:
        validate_guest_contract(image, required=["nonsense"], inspect=_present())

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "nonsense" in str(err.value)
    assert err.value.details == {
        "missing": "nonsense",
        "known": sorted(GUEST_CONTRACT_PATHS),
    }


def test_empty_required_is_a_no_op(tmp_path: Path) -> None:
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")
    validate_guest_contract(image, required=[], inspect=_present())


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, result: subprocess.CompletedProcess[str] | BaseException
) -> list[dict[str, object]]:
    """Make the real ``guestfish`` invocation return or raise ``result``; return the calls made."""
    calls: list[dict[str, object]] = []

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(validation.subprocess, "run", _run)
    return calls


def test_real_inspect_maps_missing_guestfish_to_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, FileNotFoundError("guestfish"))

    with pytest.raises(CategorizedError) as caught:
        DEFAULT_INSPECT(Path("img.qcow2"), ["/some/path"])

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert caught.value.details == {"tool": "guestfish"}


def test_real_inspect_maps_timeout_to_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_run(
        monkeypatch,
        subprocess.TimeoutExpired(cmd="guestfish", timeout=validation._GUESTFISH_TIMEOUT_S),
    )

    with pytest.raises(CategorizedError) as caught:
        DEFAULT_INSPECT(Path("img.qcow2"), ["/some/path"])

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"timeout_s": validation._GUESTFISH_TIMEOUT_S}
    # The mapped timeout_s must be the one guestfish was actually run with, not just a value the
    # error carries coincidentally — otherwise a dropped `timeout=` kwarg would still pass.
    assert calls == [
        {
            "argv": ["guestfish", "--ro", "-a", "img.qcow2", "-i"],
            "input": "exists /some/path\n",
            "capture_output": True,
            "text": True,
            "timeout": validation._GUESTFISH_TIMEOUT_S,
            "check": False,
        }
    ]


def test_real_inspect_maps_nonzero_exit_to_infrastructure_failure_with_truncated_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "x" * 2100
    _patch_run(
        monkeypatch,
        subprocess.CompletedProcess(args=["guestfish"], returncode=1, stdout="", stderr=stderr),
    )

    with pytest.raises(CategorizedError) as caught:
        DEFAULT_INSPECT(Path("img.qcow2"), ["/some/path"])

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"stderr": stderr[-2000:]}


def test_real_inspect_verdict_parsing_drops_candidates_past_the_last_reported_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If guestfish emits fewer true/false lines than requested candidates (e.g. it crashed
    # mid-script after printing some verdicts), `zip(..., strict=False)` silently truncates:
    # candidates past the last reported line are dropped rather than raising, so they come back
    # indistinguishable from an explicit "false". This test locks in that lenient behavior so a
    # future change to it is a deliberate decision, not an accidental regression.
    candidates = ["/present", "/explicitly-absent", "/never-reported"]
    calls = _patch_run(
        monkeypatch,
        subprocess.CompletedProcess(args=["guestfish"], returncode=0, stdout="true\nfalse\n"),
    )

    present = DEFAULT_INSPECT(Path("img.qcow2"), candidates)
    # All three candidates were requested, one `exists` line per candidate — only guestfish's
    # (truncated) reply drops the tail, not the request we sent it.
    assert calls[0]["input"] == (
        "exists /present\nexists /explicitly-absent\nexists /never-reported\n"
    )

    assert present == {"/present"}
