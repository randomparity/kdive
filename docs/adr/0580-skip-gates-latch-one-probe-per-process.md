# 0580 — A skip gate probes its resource once per process and latches the verdict

## Status

Accepted (2026-08-25)

## Context

The suite's skip count varied between runs on an unchanged tree, on one host, inside one
session (#2074). Four observations were recorded; the useful pair is the last two — identical
tree, no commit between them, and the counts differ by exactly one test moving from passed to
skipped. That rules out anything in a diff and rules out monotonic environment drift.

It also rules out a collection-time cause. Across every observation the collected total moves
only by the number of tests actually added, so the varying node is collected on both runs and
changes its *outcome*. That is a runtime `pytest.skip()`, not a parametrize set that differed.

The issue's leading suspect was the `--seconds` node in `tests/cli/test_verb_arg_coercion.py`,
on the theory that its parametrize set is rebuilt from live MCP tool schemas at collection
time. It is not. That skip is reached from a hardcoded `pytest.param` list over
`GENERATED_VERBS`, a committed static tuple, so it contributes exactly one skip on every run
and cannot vary. The suspect is exonerated and this record does not touch it.

What can vary is a skip gate that re-probes a live resource on every call.

- `skip_without_docker()` in `tests/support/xdist_backend.py` calls `docker_available()`,
  which constructs a `DockerClient` and pings the daemon. Nothing memoized it, so the ping ran
  once per gated test — five direct call sites, plus `shared_container_or_skip` on the
  container-acquisition path.
- `require_issuer()` in `tests/integration/live_stack/conftest.py` fetched the mock-OIDC JWKS
  with a five-second timeout on every call, from seven test modules.

Both gates ask *is the resource up right now*, and both are asked at the moment the host is
busiest, because the asking is done by a suite the gate itself runs under xdist. One ping
times out under load, one test skips, and the run is green. The only trace is a number in a
summary line nobody diffs. A gate meant to answer "this host has no Docker" answers "this
ping was slow" instead, and the two are indistinguishable downstream.

The failure mode is worse than a flake because it hides rather than fires. A flaky assertion
reddens and gets fixed. This drops a test from the run and reports success. Nor can it be
chased by re-running: three consecutive runs in the gate's own topology on an idle host
produced an identical summary line and byte-identical `-rs` blocks. It needs host load, which
is the condition under which nobody is watching the skip count.

## Decision

A skip gate that probes a live resource probes it **once per process** and reuses that verdict
for the rest of the session.

`docker_available()` holds a module-level latch. The first call probes; every later call
returns the latched answer without touching the daemon. `require_issuer()` holds the same
latch keyed by JWKS URI, next to the `_SKEW_CACHE` that already memoizes stack-skew verdicts
in that file for the same reason.

Both directions latch. Available stays available; unavailable stays unavailable.

The consequence that matters is on the available side. Once a session has latched "Docker is
up", nothing in that session can turn a Docker failure back into a skip:
`skip_without_docker()` returns without probing, and `shared_container_or_skip` re-raises the
acquisition failure instead of converting it. A daemon that dies mid-run now reddens the
suite. That is the intended trade — a run that loses its Docker daemon halfway through has
lost coverage either way, and a failure says so where a skip does not.

`KDIVE_REQUIRE_DOCKER=1` is unchanged. It remains the operator's way to demand the strict
behaviour *before* the first probe, and CI keeps setting it.

One skip that is not a probe belongs to the same shape and changes with it.
`test_minio_suspended_versioning_exposes_legacy_null_when_supported` skipped when a
versioning-suspended listing came back with no identities for a key it had written a moment
earlier. Two of that test's three skips are genuine endpoint-capability answers — the endpoint
rejects versioning suspension, or rejects version inventory — and stay skips. The
empty-listing one is not a capability answer: reaching it means the endpoint has already
proved it supports both, so a listing that omits a just-written key is a defect in the store
or in the endpoint. It fails now, and names the key.

## Consequences

The skip count becomes a function of each process's first probe rather than of per-test host
load. Under xdist every worker latches separately, so a daemon that flaps *between* worker
startups can still produce two verdicts inside one run. A daemon that flaps *during* the run
cannot. The residual is bounded by worker count rather than by test count, and closing it
needs cross-process machinery this record rejects below.

A host whose Docker daemon is down when the first gated test runs skips all of them, even if
the daemon comes up later in the same run. That is the decision, not a gap in it: one answer
per session is the property being bought.

A mid-run daemon death is now a red suite rather than a green one with fewer tests in it.
Someone will hit this on a laptop that suspends. The failure names the daemon, which is more
than the old skip did.

The latch is process state, so a test that fabricates a verdict has to put the real one back.
Both latches are reset by their test fixtures on entry *and* on exit, mirroring the existing
`_SKEW_CACHE` fixture in `tests/integration/live_stack/test_skew.py` — a fabricated verdict
left behind would be trusted by a later live test in the same worker.

Skip messages and `KDIVE_REQUIRE_DOCKER` semantics are unchanged, so `-rs` output on a host
without Docker reads exactly as before.

## Considered & rejected

**Probing eagerly in `pytest_configure`** — takes the verdict before the suite has loaded the
host, which is the best moment to ask. Rejected because it charges every pytest invocation a
Docker ping, including the unit-only runs (`just test-changed`, a single-file run) that select
no gated test at all, and because it does not remove the cross-worker residual: under xdist
`pytest_configure` runs in each worker too. Latching at first use buys the same determinism
for the runs that need it and costs nothing for the runs that do not.

**Propagating one master verdict to every xdist worker through `workerinput`** — closes the
cross-worker residual completely. Rejected as machinery out of proportion to the hazard, which
needs a daemon flapping within the few seconds that separate worker startups. Worth
revisiting if a run is ever observed carrying two verdicts.

**`functools.cache` on `docker_available`** — the same latch in one line. Rejected because
`cache_clear()` is the only reset it offers, and clearing is the wrong operation: a test that
clears mid-suite discards a live verdict and forces the next gated test to re-probe, which is
the per-test probe this record exists to remove, taken at the worst possible moment. An
explicit module attribute lets `monkeypatch` restore the *previous* verdict rather than
"unknown".

**Retrying the probe before skipping** — a bounded retry absorbs a transient timeout without
latching anything. Rejected because it lowers the probability of a spurious skip without
making the count a function of anything stable; the same report returns once the suite is
longer or the host busier. It also pays the retry on every host that genuinely has no Docker,
which is the common case for the gate.

**Turning every skip into a failure** — the strict reading of "a green suite must not silently
lose coverage". Rejected because the remaining skips are real capability answers: no Docker on
a developer laptop, no foreign qemu emulator, an endpoint that does not implement versioning
suspension. Failing those makes the suite unrunnable outside CI, which is the state
`KDIVE_REQUIRE_DOCKER=1` already exists to let an operator opt into deliberately.

**Asserting a fixed skip count in a test** — would catch the next occurrence directly.
Rejected because the count changes with every legitimately added gated test, so the assertion
would be edited on unrelated PRs until someone stopped reading it, and because it reports the
symptom one run after the coverage was already lost.
