# ADR 0482 — Surface the deployed build on `/readyz` and preflight the live-stack tier against it

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** #1630
- **Extends:** [ADR-0090](0090-opentelemetry-adoption-service-health.md) §5, which defines the aux
  health/metrics listener and its loopback/pod-local trust boundary. That boundary is
  unchanged here; only the `/readyz` body grows a field.
- **Depends on:** [ADR-0041](0041-versioning-release-process.md) and
  [ADR-0370](0370-container-image-version-provenance.md), which together make `version_info()` the
  single source of the running process's commit — baked `_buildinfo` first, live git second,
  unknown last.

## Context

A running kdive deployment says nothing about how far behind the working tree it is. While
proving #1610 the `kdive-demo` k3s deployment was serving an image built 3,742 commits before
`main`, and nothing in its behaviour said so. Tool calls failed because the tool did not exist
yet in the deployed image; a `systems.toml` the deployed loader could not parse read as a
schema bug. Both cost real time because **a stale deployment is symptomatically identical to a
defect**.

The same trap has a local variant that bit the same run. The live stack's app tier is three
plain Python processes started by `scripts/live-stack/up.sh` (`restart_host_processes`, which
already prints the checkout's short SHA to the console and then discards it). They have no hot
reload and no supervisor, so an edited source file does not reach a running process until it is
restarted. A spine was driven against a worker that predated its own fix and the resulting green
meant nothing.

`version_info()` already resolves the answer. Its three consumers — `kdive --version`, the
startup log line, and `service_version` inside the `ops.diagnostics` envelope — are all
*reporting* surfaces; none is comparative.

There is one piece of prior art, and it is the right shape: `report_build_stamps()` in
`scripts/live-stack/lib.sh` greps each process's `starting kdive …` startup line out of
`.live-stack-logs/` and prints it under `=== build stamps (expect g<head_sha>) ===`. It proves
the comparison is worth making. What it cannot do is *gate* anything — it is a human-read
console banner emitted by a bring-up script, not a fact the test suite consumes, so a stale
stack still runs the whole live tier and reports a meaningless result.

A log-scraping preflight was the obvious extension of it and was considered. It reads
`started_at` (the log `ts`) and the commit from a line kdive already emits, needs no change to
any network surface, and — unlike an HTTP probe — still answers for a process that has since
died. It was rejected because it only works where the log files are: it is coupled to
`.live-stack-logs/` paths and a `KDIVE_STACK_LOG_DIR` layout that exists solely for the
host-process bring-up, and it answers nothing at all for a container or a pod, which is the
deployment shape that caused #1610 in the first place. `/readyz` answers for both, over a
surface every deployment already exposes.

## Decision

### 1. `/readyz` carries the deployed build; no new tool, and not `ops.diagnostics`

The aux listener's `/readyz` body grows one additive `version` object:

```json
{"ready": true, "checks": {...},
 "version": {"version": "0.3.0", "commit": "9f3c1ab", "is_release": false,
             "started_at": "2026-07-28T17:04:11Z"}}
```

Three alternatives were considered and rejected:

- **A new MCP tool.** Rejected outright. Epic #1576 just cut the tool surface 140 → 123, and a
  new tool also has to be threaded through `exposure.py`, `_BEHAVIOR_TESTS_BY_TOOL`, and the
  RBAC matrix. A skew probe is infrastructure, not agent capability.
- **Routing through `ops.diagnostics`.** It already carries `service_version`, but it is a
  mutating, RBAC-gated verb. A preflight that has to mint an operator token and issue a
  mutating call before it can decide whether to run is a preflight nobody keeps.
- **`/livez`.** Its body is a bare `ok`/`stale` liveness token that a kubelet and a compose
  healthcheck both parse. `/readyz` is already a JSON document with a growable shape.

`version` is a sibling of `ready` and `checks`, so no existing reader of either key changes.
Critically, `/readyz` returns the **same body on 503 as on 200**, so the deployed build is
readable from a stack whose backends are down — which is exactly when an operator is trying to
decide whether they are debugging a defect or a stale process.

The values come from `version_info()` (ADR-0041/0370), so the aux listener adds no fourth
resolution path. `started_at` is captured when the app is built — once per process, at startup —
and is what makes decision 3 possible.

### 2. The trust boundary is unchanged, and that is what makes this safe

ADR-0090 §5 binds the aux listener loopback by default; compose never publishes the aux port to
the host and no Helm `Service` fronts it. The endpoint carries no authentication because **the
network boundary is its access control**, and this ADR does not widen it.

That boundary is why exposing a commit SHA here is acceptable. `/readyz` already discloses the
process's backend topology through `checks`; a build identifier is a strictly smaller fact, and
it is disclosed to exactly the same set of callers — a kubelet, an in-network scrape, and a
process on the same host. It is not reachable from the public MCP port.

### 3. Skew tolerance: five verdicts, not an equality test

`commit != HEAD` is unusable. On any working checkout it fires constantly, and a preflight that
cries wolf is ignored within a day and deleted within a week. The comparison is defined over
git ancestry instead, with the deployed short SHA resolved through `git rev-parse` first so that
the baked 12-character form (`stamp-buildinfo.sh --short=12`) and the live-git default width
compare correctly:

| verdict | condition | severity |
|---|---|---|
| `fresh` | deployed commit == `HEAD`, and no `src/kdive` file *differing from `HEAD`* is newer than `started_at` | pass, silent |
| `stale_restart` | deployed commit == `HEAD`, but an **uncommitted** `src/kdive` change is newer than `started_at` | **skip** |
| `behind` | deployed commit is an ancestor of `HEAD` | warn |
| `diverged` | deployed commit is a real commit but not an ancestor of `HEAD`, or is unknown to this repo | warn |
| `unknown` | the process reports no commit, or no aux listener answered | warn |

`stale_restart` is the only verdict that skips, and the asymmetry is deliberate: it is the
local variant from #1610 and its remedy is one command (`scripts/live-stack/up.sh`), so skipping
is *more* informative than letting the test proceed — the skip reason names the fix, the eventual
failure would not.

Its precision rests on one detail that is easy to get wrong. A bare mtime walk of `src/kdive`
is **not** a staleness signal: a `git worktree add` stamps every file `now`, and a branch
round-trip, a stash pop, or an identical reformat rewrite mtimes while leaving content
byte-identical to `HEAD`. Both were reproduced; either would have produced a false
`stale_restart` — and since this is the verdict that skips, a false positive silently deletes
the whole live tier, the exact failure this ADR refuses to accept for `behind` two paragraphs
down. In this repo's worktree-per-agent workflow it would have fired on essentially every run.

So the mtime is only consulted for files git reports as *differing from `HEAD`*
(`git diff --name-only HEAD -- src/kdive`). The deployed commit is already known to equal
`HEAD` at that point, so a clean tree means the process is running the code on disk whatever
the timestamps say, and the verdict is `fresh`. Only an uncommitted edit newer than the process
start is evidence, and there it is conclusive.

Two residual gaps, stated rather than papered over. A process started against a dirty tree that
was later `git restore`d reads `fresh` though it still holds the discarded code. And the
comparison is against the checkout the *tests* run from, which in a worktree workflow need not
be the checkout the stack was started from — a genuine difference then surfaces as
`behind`/`diverged` (correct, and warn-level), but an identical-commit mismatch is invisible.

`behind` and `diverged` only warn, because both can be legitimate: deliberately exercising an
older deployment, or running from a branch the deployment does not contain. Turning either into
a skip would silently delete a whole live tier during a rebase, which is a worse failure than
the one being prevented.

`KDIVE_STACK_SKEW_POLICY` takes `off` (probe nothing), `warn` (never skip; downgrade
`stale_restart`), or `strict` (skip on every non-`fresh` verdict). The default is the table
above. One knob with three values is the minimum that lets an operator get past a misfiring
gate without deleting it — a gate with no escape hatch is a gate that gets removed.

The probe is memoized per session: `require_stack()` is called from dozens of tests, and the
skew answer cannot change while a process keeps running.

### 4. Migration skew is explicitly OUT of scope

#1610 also found the deployed database 34 migrations behind (45 applied vs 79 in tree). That is
a real second signal and it is **not** delivered here.

It is not nearly free. Migration state lives in the database, so surfacing it on `/readyz` means
adding a database query to an unauthenticated endpoint that a kubelet polls on a liveness
cadence — precisely the "unauthenticated `/readyz` that triggers backend calls" that ADR-0090 §5
calls out. Doing it properly means either a cached revision read at startup (a different
lifecycle from `started_at`, since migrations can be applied to a running stack) or a separate
authenticated surface. Either is its own decision.

The narrower reason it is not urgent: `scripts/live-stack/up.sh` applies migrations from the
host checkout on every bring-up and fails loudly on divergence, so the local tier this preflight
guards cannot drift the way the k3s deployment did.

## Consequences

- A stale live stack is named at preflight time instead of being rediscovered through a
  confusing test failure — the #1610 cost, closed for the tier that consumes it.
- `/readyz` gains one field. Compose healthchecks and kubelet probes read status codes and are
  unaffected; the compose "aux port is never published" and Helm "no Service fronts the aux
  port" contracts are unchanged and still asserted.
- The preflight is only as good as its reach. It works for the live-stack tier because that
  tier's app processes run on the same host as the tests, where the loopback aux listener is
  reachable — verified, all three ports answer. It does **not** reach a k3s deployment from
  outside the cluster, because no `Service` fronts the aux port. Surfacing skew for a genuinely
  remote deployment needs a decision about exposing an authenticated build identifier on the
  public port, and is not made here.
- `stale_restart` costs one `git diff --name-only HEAD -- src/kdive` plus a `stat` of each
  changed file, once per session.
- The probe degrades to `unknown` (warn, never skip) for any process that does not answer or
  predates this ADR, so it never blocks an older deployment.
