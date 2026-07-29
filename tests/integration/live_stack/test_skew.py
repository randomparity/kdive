"""The live-stack skew grading (ADR-0482 §3, issue #1630).

Non-gated: these carry no live marker, so they run in the default `just test` and pin the
preflight's grading and policy tables without a stack. The point of the design under test is
that `commit != HEAD` is *not* the rule — a working checkout would trip that constantly — so
these assert the tolerance rather than an equality.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta

import pytest
from _pytest.outcomes import Skipped

from tests.integration.live_stack import conftest
from tests.integration.live_stack.skew import (
    POLICY_ENV,
    ProcessSkew,
    RepoFacts,
    SkewPolicy,
    SkewVerdict,
    classify,
    partition,
    readyz_urls,
    skew_policy,
    skipping_verdicts,
)

_HEAD = "a" * 40
_OLDER = "b" * 40
_OTHER = "c" * 40

_STARTED = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
_STARTED_AT = "2026-07-28T12:00:00Z"


def _facts(
    *,
    head: str = _HEAD,
    known: dict[str, str] | None = None,
    ancestors: set[tuple[str, str]] | None = None,
    count: int | None = 7,
    newest_mtime: float | None = None,
) -> RepoFacts:
    resolved = known if known is not None else {_HEAD: _HEAD, _OLDER: _OLDER, _OTHER: _OTHER}
    edges = ancestors if ancestors is not None else {(_OLDER, _HEAD)}
    # Default: last edit one hour BEFORE startup, i.e. the process loaded the code on disk.
    before_start = (_STARTED - timedelta(hours=1)).timestamp()
    mtime = newest_mtime if newest_mtime is not None else before_start
    return RepoFacts(
        head=head,
        resolve=lambda c: resolved.get(c),
        is_ancestor=lambda a, d: (a, d) in edges,
        commits_between=lambda _a, _d: count,
        newest_source_mtime=lambda: mtime,
    )


def test_head_with_older_source_is_fresh() -> None:
    result = classify("server", commit=_HEAD, started_at=_STARTED_AT, facts=_facts())
    assert result.verdict is SkewVerdict.FRESH


def test_head_with_source_edited_after_startup_is_stale_restart() -> None:
    # The #1630 local variant: the process is at HEAD, so an equality check would call it
    # clean, but a source file changed after it started — it is not running the code on disk.
    edited = (_STARTED + timedelta(minutes=5)).timestamp()
    result = classify(
        "worker", commit=_HEAD, started_at=_STARTED_AT, facts=_facts(newest_mtime=edited)
    )
    assert result.verdict is SkewVerdict.STALE_RESTART
    assert "300s after this process started" in result.detail
    assert "scripts/live-stack/up.sh" in result.detail


def test_ancestor_commit_is_behind_and_names_the_distance() -> None:
    result = classify("server", commit=_OLDER, started_at=_STARTED_AT, facts=_facts(count=3742))
    assert result.verdict is SkewVerdict.BEHIND
    assert "3742 commits behind HEAD" in result.detail


def test_behind_survives_an_uncountable_distance() -> None:
    result = classify("server", commit=_OLDER, started_at=_STARTED_AT, facts=_facts(count=None))
    assert result.verdict is SkewVerdict.BEHIND
    assert "unknown number of commits" in result.detail


def test_non_ancestor_commit_is_diverged() -> None:
    result = classify("server", commit=_OTHER, started_at=_STARTED_AT, facts=_facts())
    assert result.verdict is SkewVerdict.DIVERGED


def test_commit_unknown_to_the_repository_is_diverged() -> None:
    result = classify("server", commit="deadbeef", started_at=_STARTED_AT, facts=_facts())
    assert result.verdict is SkewVerdict.DIVERGED
    assert "not a commit in this repository" in result.detail


def test_missing_commit_is_unknown() -> None:
    assert classify("server", commit=None, started_at=_STARTED_AT, facts=_facts()).verdict is (
        SkewVerdict.UNKNOWN
    )


def test_head_with_unparseable_start_time_is_unknown_not_fresh() -> None:
    # Failing open to FRESH here would silently disable the stale_restart signal for any
    # process whose clock stamp is malformed.
    result = classify("server", commit=_HEAD, started_at="not-a-time", facts=_facts())
    assert result.verdict is SkewVerdict.UNKNOWN


def test_a_short_deployed_sha_is_resolved_before_comparison() -> None:
    # The baked build stamps --short=12 while live git uses its own width, so the grading must
    # compare resolved full SHAs. A raw string compare would call this DIVERGED.
    facts = _facts(known={"a" * 12: _HEAD})
    result = classify("server", commit="a" * 12, started_at=_STARTED_AT, facts=facts)
    assert result.verdict is SkewVerdict.FRESH


# --- policy table ---------------------------------------------------------------------------


def test_default_policy_skips_only_stale_restart() -> None:
    # The asymmetry is the design (ADR-0482 §3): behind/diverged can be legitimate, so turning
    # them into skips would silently delete a live tier during a rebase.
    assert skipping_verdicts(SkewPolicy.DEFAULT) == frozenset({SkewVerdict.STALE_RESTART})


def test_warn_policy_skips_nothing() -> None:
    assert skipping_verdicts(SkewPolicy.WARN) == frozenset()


def test_strict_policy_skips_every_non_fresh_verdict() -> None:
    skipping = skipping_verdicts(SkewPolicy.STRICT)
    assert SkewVerdict.FRESH not in skipping
    assert skipping == frozenset(v for v in SkewVerdict if v is not SkewVerdict.FRESH)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", SkewPolicy.OFF),
        ("warn", SkewPolicy.WARN),
        ("strict", SkewPolicy.STRICT),
        ("STRICT", SkewPolicy.STRICT),
        (" warn ", SkewPolicy.WARN),
        ("", SkewPolicy.DEFAULT),
        ("nonsense", SkewPolicy.DEFAULT),
    ],
)
def test_policy_env_parsing(raw: str, expected: SkewPolicy) -> None:
    assert skew_policy({POLICY_ENV: raw}) is expected


def test_policy_defaults_when_env_is_absent() -> None:
    assert skew_policy({}) is SkewPolicy.DEFAULT


def test_partition_splits_skip_from_warn_and_drops_fresh() -> None:
    results = [
        ProcessSkew("server", SkewVerdict.FRESH, "ok"),
        ProcessSkew("worker", SkewVerdict.STALE_RESTART, "edited"),
        ProcessSkew("reconciler", SkewVerdict.BEHIND, "old"),
    ]
    skip, warn = partition(results, SkewPolicy.DEFAULT)
    assert [r.process for r in skip] == ["worker"]
    assert [r.process for r in warn] == ["reconciler"]


def test_partition_under_warn_policy_never_skips() -> None:
    results = [ProcessSkew("worker", SkewVerdict.STALE_RESTART, "edited")]
    skip, warn = partition(results, SkewPolicy.WARN)
    assert skip == []
    assert [r.process for r in warn] == ["worker"]


# --- aux URL derivation ---------------------------------------------------------------------


def test_readyz_urls_cover_all_three_processes_on_the_stack_host() -> None:
    # All three, not just the server: the #1630 local variant was a stale *worker*, which a
    # server-only probe would miss entirely.
    urls = readyz_urls("http://127.0.0.1:8000/mcp")
    assert urls == {
        "reconciler": "http://127.0.0.1:9466/readyz",
        "server": "http://127.0.0.1:9464/readyz",
        "worker": "http://127.0.0.1:9465/readyz",
    }


def test_readyz_urls_follow_a_relocated_stack_host() -> None:
    urls = readyz_urls("http://kdive.internal:18000/mcp")
    assert urls["server"] == "http://kdive.internal:9464/readyz"


# --- the require_stack() seam ---------------------------------------------------------------
#
# The grading above is worthless if the gate never runs. These drive the real preflight every
# live_stack test goes through, with only the network probe faked.

_STACK_URL = "http://127.0.0.1:8000/mcp"


@pytest.fixture
def stack_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_STACK_BASE_URL", _STACK_URL)
    monkeypatch.delenv(POLICY_ENV, raising=False)
    conftest._SKEW_CACHE.clear()


def _fake_probe(*results: ProcessSkew) -> object:
    return lambda _base_url: list(results)


def test_require_stack_skips_on_a_stale_restart(
    stack_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        conftest,
        "probe_stack_skew",
        _fake_probe(ProcessSkew("worker", SkewVerdict.STALE_RESTART, "edited after start")),
    )
    with pytest.raises(Skipped) as excinfo:
        conftest.require_stack()
    message = str(excinfo.value)
    assert "worker" in message
    # The skip must carry both the diagnosis and the escape hatch, or it is just a mystery.
    assert "edited after start" in message
    assert f"{POLICY_ENV}=warn" in message


def test_require_stack_warns_but_runs_when_merely_behind(
    stack_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        conftest,
        "probe_stack_skew",
        _fake_probe(ProcessSkew("server", SkewVerdict.BEHIND, "12 commits behind HEAD")),
    )
    with pytest.warns(UserWarning, match="12 commits behind HEAD"):
        assert conftest.require_stack() == _STACK_URL


def test_require_stack_is_silent_on_a_fresh_stack(
    stack_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        conftest, "probe_stack_skew", _fake_probe(ProcessSkew("server", SkewVerdict.FRESH, "ok"))
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert conftest.require_stack() == _STACK_URL


def test_require_stack_probes_once_per_session(
    stack_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # require_stack() is called from dozens of tests; an unmemoized probe would issue three
    # HTTP requests per call.
    calls: list[str] = []

    def counting(base_url: str) -> list[ProcessSkew]:
        calls.append(base_url)
        return [ProcessSkew("server", SkewVerdict.FRESH, "ok")]

    monkeypatch.setattr(conftest, "probe_stack_skew", counting)
    conftest.require_stack()
    conftest.require_stack()
    conftest.require_stack()
    assert calls == [_STACK_URL]


def test_policy_off_does_not_probe_at_all(stack_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(POLICY_ENV, "off")

    def explode(_base_url: str) -> list[ProcessSkew]:
        raise AssertionError("probe must not run under KDIVE_STACK_SKEW_POLICY=off")

    monkeypatch.setattr(conftest, "probe_stack_skew", explode)
    assert conftest.require_stack() == _STACK_URL


def test_require_stack_still_skips_on_a_missing_url_before_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KDIVE_STACK_BASE_URL", raising=False)
    conftest._SKEW_CACHE.clear()
    with pytest.raises(Skipped, match="KDIVE_STACK_BASE_URL unset"):
        conftest.require_stack()
