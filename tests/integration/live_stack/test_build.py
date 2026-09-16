"""Non-gated shape guard for the shared live-stack build profile (#2511, ADR-0665).

The live-stack suites send one ``build_profile`` document over the wire, built by
``spine.build_profile``. These tests are the single place that document is checked against the
real ``BuildProfile`` validator, so a model change cannot pass while the suites still send the
old shape. They carry no live marker and run in CI, where the suites themselves skip.

The module is named ``test_build`` rather than ``test_build_profile`` on purpose:
``scripts/select_changed_tests.py`` maps a changed source file onto ``tests/**/test_<stem>.py``,
so only this stem makes a ``src/kdive/profiles/build.py`` change select the guard that exists to
catch exactly that drift (ADR-0665).
"""

from __future__ import annotations

import pytest

from kdive.profiles.build import BuildProfile
from tests.integration.live_stack.spine import build_profile

# The arches the suites actually send: x86_64 from the three former `_build_profile()` factories,
# ppc64le from the three inline documents in `test_live_stack.py` (#1144/#1146, ADR-0636).
_SENT_ARCHES = ("x86_64", "ppc64le")


@pytest.mark.parametrize("arch", _SENT_ARCHES)
def test_the_shared_build_profile_parses_through_the_real_validator(arch: str) -> None:
    """The document the suites send is one ``BuildProfile.parse`` accepts, for each sent arch."""
    profile = BuildProfile.parse(build_profile(arch))

    assert profile.schema_version == 1
    assert profile.arch == arch


def test_the_shared_build_profile_carries_only_known_required_fields() -> None:
    """Every key is a ``BuildProfile`` field, and every field the model requires is present.

    Deliberately not key-set equality. A field arriving with a default leaves the existing
    document valid, so whether the suites should start sending it is a judgment call, not a
    regression — equality would red on it and direct the wrong fix (ADR-0665).
    """
    fields = BuildProfile.model_fields
    required = {name for name, field in fields.items() if field.is_required()}
    keys = set(build_profile())

    assert keys <= set(fields), f"not BuildProfile fields: {sorted(keys - set(fields))}"
    assert required <= keys, f"required fields missing: {sorted(required - keys)}"


def test_the_shared_build_profile_is_the_document_the_suites_sent() -> None:
    """The literal is pinned to what the six former restatements sent.

    This is the guard's independent oracle. Deriving the document from ``BuildProfile`` instead
    would make the parse test above tautological: the suites would send whatever the model says,
    and a model change could never be observed as drift (ADR-0665).
    """
    assert build_profile() == {"schema_version": 1, "arch": "x86_64"}
    assert build_profile("ppc64le") == {"schema_version": 1, "arch": "ppc64le"}
