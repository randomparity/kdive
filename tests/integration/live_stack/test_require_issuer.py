"""Unit coverage for the require_issuer skip gate's session latch (#2074, ADR-0580)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _pytest.outcomes import Skipped

from kdive.cli.login import OidcIssuer
from tests.integration.live_stack import conftest

_ISSUER_URL = "http://issuer.test.kdive/default"


class _CountingReachability:
    """A stand-in for the JWKS fetch that records how often it was asked.

    Answers are consumed in order and the last one repeats, so ``_CountingReachability(
    True, False)`` models an issuer that answers at session start and then stops — the
    #2074 condition. A call count of one is the assertion that matters.
    """

    def __init__(self, *answers: bool) -> None:
        self._answers = answers
        self.calls = 0

    def __call__(self, _issuer: OidcIssuer) -> bool:
        self.calls += 1
        return self._answers[min(self.calls - 1, len(self._answers) - 1)]


@pytest.fixture
def issuer_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KDIVE_OIDC_ISSUER", _ISSUER_URL)
    monkeypatch.setattr(conftest, "oidc_issuer_from_env", lambda: OidcIssuer(base_url=_ISSUER_URL))
    conftest._ISSUER_REACHABLE.clear()
    yield
    # Clear on the way out too: these tests seed the *real* conftest cache with fabricated
    # verdicts, which a later live_stack run in the same process would otherwise trust.
    conftest._ISSUER_REACHABLE.clear()


def test_reachability_is_probed_once_and_reused(
    issuer_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #2074 property: an issuer that answered cannot lose a test to a slow fetch.

    The ``Skipped`` catch is load-bearing. A regression re-probes, the second call answers
    False, and the gate skips *this* test — which would leave the suite green while the
    coverage silently went away, the exact shape under test.
    """
    reachable = _CountingReachability(True, False)
    monkeypatch.setattr(conftest, "_issuer_reachable", reachable)

    first = conftest.require_issuer()
    try:
        second = conftest.require_issuer()
    except Skipped as exc:
        pytest.fail(f"a latched-reachable verdict must never skip a later test: {exc}")

    assert (first.base_url, second.base_url) == (_ISSUER_URL, _ISSUER_URL)
    assert reachable.calls == 1


def test_an_unreachable_verdict_latches_too(
    issuer_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An issuer that comes up mid-run does not un-skip the tests already skipped."""
    reachable = _CountingReachability(False, True)
    monkeypatch.setattr(conftest, "_issuer_reachable", reachable)

    for _ in range(2):
        with pytest.raises(Skipped, match="JWKS unreachable"):
            conftest.require_issuer()

    assert reachable.calls == 1


def test_a_missing_issuer_url_still_skips_before_probing(
    issuer_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    reachable = _CountingReachability(True)
    monkeypatch.setattr(conftest, "_issuer_reachable", reachable)
    monkeypatch.delenv("KDIVE_OIDC_ISSUER")

    with pytest.raises(Skipped, match="KDIVE_OIDC_ISSUER unset"):
        conftest.require_issuer()

    assert reachable.calls == 0
    assert conftest._ISSUER_REACHABLE == {}

    # Restored, the very next call probes for real — proving the count above was the
    # unset-URL short-circuit and not a latch this test had already taken.
    monkeypatch.setenv("KDIVE_OIDC_ISSUER", _ISSUER_URL)
    assert conftest.require_issuer().base_url == _ISSUER_URL
    assert reachable.calls == 1
