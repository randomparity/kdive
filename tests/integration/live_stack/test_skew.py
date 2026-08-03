"""The live-stack skew grading (ADR-0482 §3, issue #1630).

Non-gated: these carry no live marker, so they run in the default `just test` and pin the
preflight's grading and policy tables without a stack. The point of the design under test is
that `commit != HEAD` is *not* the rule — a working checkout would trip that constantly — so
these assert the tolerance rather than an equality.
"""

from __future__ import annotations

import re
import socket
import socketserver
import subprocess
import threading
import time
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from _pytest.outcomes import Skipped

from kdive.health.aux_listener import build_aux_app
from kdive.health.heartbeat import Heartbeat
from kdive.health.probe import BackendCheck, HealthProbe
from kdive.version import version_info
from tests.integration.live_stack import conftest, skew
from tests.integration.live_stack.skew import (
    POLICY_ENV,
    ProcessSkew,
    RepoFacts,
    SkewPolicy,
    SkewVerdict,
    classify,
    partition,
    probe_stack_skew,
    readyz_urls,
    repo_facts,
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
    # Default: a clean tree — no file differs from HEAD, so there is no staleness signal.
    return RepoFacts(
        head=head,
        resolve=lambda c: resolved.get(c),
        is_ancestor=lambda a, d: (a, d) in edges,
        commits_between=lambda _a, _d: count,
        newest_modified_source_mtime=lambda: newest_mtime,
    )


def test_head_with_a_clean_tree_is_fresh() -> None:
    result = classify("server", commit=_HEAD, started_at=_STARTED_AT, facts=_facts())
    assert result.verdict is SkewVerdict.FRESH


def test_head_with_an_uncommitted_edit_made_before_startup_is_fresh() -> None:
    # The process started after the edit, so it loaded it. Dirty is not by itself stale.
    edited = (_STARTED - timedelta(minutes=5)).timestamp()
    result = classify(
        "server", commit=_HEAD, started_at=_STARTED_AT, facts=_facts(newest_mtime=edited)
    )
    assert result.verdict is SkewVerdict.FRESH


def test_head_with_a_clean_tree_is_fresh_however_new_the_timestamps() -> None:
    # The false positive that would silently delete the live tier: `git worktree add`, a branch
    # round-trip and a stash pop all stamp every file `now` while leaving content identical to
    # HEAD. A clean tree yields no modified file, so mtimes never enter the grading at all.
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
    assert "uncommitted source" in result.detail
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
    # Enumerated, not derived from the same comprehension the implementation uses — a derived
    # expectation auto-adapts to a new verdict and can only fail if that source line is edited.
    assert skipping_verdicts(SkewPolicy.STRICT) == frozenset(
        {
            SkewVerdict.STALE_RESTART,
            SkewVerdict.BEHIND,
            SkewVerdict.DIVERGED,
            SkewVerdict.UNKNOWN,
        }
    )


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


def test_readyz_urls_cover_all_processes_on_the_stack_host() -> None:
    # Every process, not just the server: the #1630 local variant was a stale *worker*, which a
    # server-only probe would miss entirely.
    urls = readyz_urls("http://127.0.0.1:8000/mcp")
    assert urls == {
        "lifecycle-witness": "http://127.0.0.1:9467/readyz",
        "reconciler": "http://127.0.0.1:9466/readyz",
        "server": "http://127.0.0.1:9464/readyz",
        "worker": "http://127.0.0.1:9465/readyz",
    }


def test_readyz_urls_follow_a_relocated_stack_host() -> None:
    urls = readyz_urls("http://kdive.internal:18000/mcp")
    assert urls["server"] == "http://kdive.internal:9464/readyz"


def test_readyz_urls_bracket_an_ipv6_host() -> None:
    # urlsplit().hostname strips the brackets; re-emitting them bare yields "http://::1:9464".
    assert readyz_urls("http://[::1]:8000/mcp")["server"] == "http://[::1]:9464/readyz"


def test_readyz_urls_honor_an_explicit_health_bind_addr() -> None:
    # An explicit KDIVE_HEALTH_BIND_ADDR wins for every process (ADR-0090 §5), so the
    # per-process default map does not apply and probing it would hit the wrong port.
    urls = readyz_urls("http://127.0.0.1:8000/mcp", {"KDIVE_HEALTH_BIND_ADDR": "0.0.0.0:7000"})
    assert urls == {"stack": "http://127.0.0.1:7000/readyz"}


# --- the network and git layers ---------------------------------------------------------------
#
# The grading above is pure. Everything that actually reaches a process or a repository is
# exercised here against a real socket and this real checkout — otherwise the feature could be
# wholly non-functional and still look green, since its only failure signal is a warning.


def _serve(app: object) -> Iterator[str]:
    """Serve ``app`` on an ephemeral loopback port; yield its base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")  # ty: ignore
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20.0)


@pytest.fixture
def ready_stack() -> Iterator[str]:
    app = build_aux_app(
        heartbeat=Heartbeat(stale_after=1e9), probe=HealthProbe(checks=[]), metric_reader=None
    )
    yield from _serve(app)


@pytest.fixture
def unready_stack() -> Iterator[str]:
    async def down() -> None:
        raise RuntimeError("pg down")

    app = build_aux_app(
        heartbeat=Heartbeat(stale_after=1e9),
        probe=HealthProbe(checks=[BackendCheck(name="pg", probe=down)]),
        metric_reader=None,
    )
    yield from _serve(app)


def test_fetch_version_reads_the_real_aux_listener(ready_stack: str) -> None:
    # Over a real socket against the real app: proves the produced body and the consumer agree.
    version = skew._fetch_version(f"{ready_stack}/readyz")
    assert version is not None
    assert version["commit"] == version_info().commit
    assert set(version) == {"version", "commit", "is_release", "started_at"}


def test_fetch_version_reads_the_body_of_a_503(unready_stack: str) -> None:
    # The load-bearing edge (ADR-0482 §1): urllib raises HTTPError on 503, so without the
    # re-read path the preflight goes blind exactly when a backend is down.
    version = skew._fetch_version(f"{unready_stack}/readyz")
    assert version is not None
    assert version["commit"] == version_info().commit


def test_fetch_version_is_none_on_a_refused_port() -> None:
    # The socket is held bound — but never listening — across the assertion. A bound port
    # cannot be handed to anything else, and a connect with no listen backlog behind it is
    # refused, so "nothing answers here" holds by construction. Binding and *closing* first
    # released the port back to the ephemeral range, where a busy host's wildcard listeners
    # can answer instead (#1713).
    with socket.socket() as refusing:
        refusing.bind(("127.0.0.1", 0))
        port = refusing.getsockname()[1]
        assert skew._fetch_version(f"http://127.0.0.1:{port}/readyz") is None


def test_fetch_version_is_none_on_a_non_http_listener() -> None:
    # A squatter on the aux port raises BadStatusLine, which is neither an OSError nor wrapped
    # by urlopen — uncaught, it would error every live_stack test rather than degrade.
    with socketserver.TCPServer(("127.0.0.1", 0), _GarbageHandler) as server:
        threading.Thread(target=server.handle_request, daemon=True).start()
        port = server.server_address[1]
        assert skew._fetch_version(f"http://127.0.0.1:{port}/readyz") is None


class _GarbageHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.recv(4096)
        self.request.sendall(b"GARBAGE NOT HTTP\r\n\r\n")


def test_probe_stack_skew_degrades_to_unknown_when_nothing_answers() -> None:
    # A stack that predates this feature, or is simply down, must warn — never skip, never
    # raise. This is the backward-compatibility contract.
    #
    # The no-answer case is injected through the transport seam rather than staged on a port
    # (#1713): the previous form bound an ephemeral port, closed it, and assumed the OS would
    # leave it quiet, which on a host with wildcard listeners in that range it does not.
    probed: list[str] = []

    def nothing_answers(url: str) -> dict[str, object] | None:
        probed.append(url)
        return None

    base = "http://127.0.0.1:8000/mcp"
    results = probe_stack_skew(base, fetch=nothing_answers)
    assert {r.verdict for r in results} == {SkewVerdict.UNKNOWN}
    assert [r.process for r in results] == [
        "lifecycle-witness",
        "reconciler",
        "server",
        "worker",
    ]
    # The injected transport is the one consulted, for every process — a `fetch` argument
    # accepted and then ignored would still satisfy the verdicts above on a host with no stack.
    # Derived from readyz_urls, which has its own tests: the port table is pinned there, and
    # restating it here would only redden two tests for one edit.
    assert probed == list(readyz_urls(base).values())


def test_repo_facts_describe_this_actual_checkout() -> None:
    facts = repo_facts()
    assert facts is not None
    assert re.fullmatch(r"[0-9a-f]{40}", facts.head)
    # A real short SHA resolves to the same full SHA — the width-tolerance the grading needs.
    assert facts.resolve(facts.head[:9]) == facts.head
    assert facts.is_ancestor(facts.head, facts.head)
    assert facts.commits_between(facts.head, facts.head) == 0


def test_repo_facts_reject_a_ref_name_reported_as_a_commit() -> None:
    facts = repo_facts()
    assert facts is not None
    # "HEAD"/"main" would otherwise resolve against *this* checkout and grade a foreign
    # responder as clean.
    assert facts.resolve("HEAD") is None
    assert facts.resolve("main") is None
    assert facts.resolve("") is None


def test_repo_facts_report_no_modified_source_on_a_clean_tree() -> None:
    facts = repo_facts()
    assert facts is not None
    dirty = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "src/kdive"],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        pytest.skip(f"src/kdive has uncommitted changes: {dirty.splitlines()}")
    # The P1 guarantee: a clean tree yields no timestamp at all, so no mtime — however recently
    # rewritten by a worktree add or a branch round-trip — can produce a stale_restart skip.
    assert facts.newest_modified_source_mtime() is None


# --- the require_stack() seam ---------------------------------------------------------------
#
# The grading above is worthless if the gate never runs. These drive the real preflight every
# live_stack test goes through, with only the network probe faked.

_STACK_URL = "http://127.0.0.1:8000/mcp"


@pytest.fixture
def stack_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KDIVE_STACK_BASE_URL", _STACK_URL)
    monkeypatch.delenv(POLICY_ENV, raising=False)
    conftest._SKEW_CACHE.clear()
    yield
    # Clear on the way out too: these tests seed the *real* conftest cache with fabricated
    # verdicts, which a later live_stack run in the same process would otherwise trust.
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
