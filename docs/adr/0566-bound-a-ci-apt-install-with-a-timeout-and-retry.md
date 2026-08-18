# 0566 — Bound a CI apt install with a hard timeout, then retry it

## Status

Accepted (2026-08-17)

## Context

`Install libvirt build headers` wedged twice in one afternoon: 33 minutes on the
`lint · type · test` job of #1972 and 13 minutes on `supply chain (runtime)` of #1977, against a
normal of about 15 seconds. Both were unstuck by a human cancelling the job and re-running it,
and both reruns finished in ~51s with no code change. A concurrent sibling PR ran the identical
step in 83s during the second occurrence, so this was not a repo-wide Actions outage — it was
one runner's connection to one mirror.

`libvirt-dev` cannot be dropped: `libvirt-python` publishes no wheels and compiles against the
system headers, so `uv sync` fails without it. Six steps across four workflows install it or a
superset of it.

**The observed symptom is a stall, not a non-zero exit.** `apt-get` did not return at all. That
is the fact that decides the design, because the obvious fix — a retry-on-exit-status wrapper,
the shape ADR-0553 established for `docker pull` — would not have fired on either occurrence.
A retry loop only ever sees a command that finished.

It also decides where the cost lands. None of `ci.yml`, `test-ordering.yml` or
`mcp-spec-drift.yml` declared `timeout-minutes` at any level, so a wedged step is bounded only
by the GitHub Actions default of **360 minutes**. What ended these two occurrences was a human
noticing.

ADR-0553 already settles the retry half. Its *Considered & rejected* names `apt-get` first among
the single-shot network calls it deliberately left out, "tracked separately"; this is that
follow-up. What ADR-0553 does not cover is the stall — it reasons entirely about exit statuses
and what they do and do not encode. This record exists for that gap.

## Decision

**Bound each `apt-get` call with a hard `timeout`, then retry the bounded call.** The timeout is
what turns a stall into a failure; the retry is what keeps that failure from being fatal. Either
half alone is not a fix: retry alone never fires on a hang, and a timeout alone converts an
intermittent slow mirror into an intermittent red check.

`scripts/apt-install.sh` is the single implementation, invoked by all six call sites. It runs
`apt-get update` then `apt-get install` under `sudo timeout --kill-after=10s`, three attempts
with 5s/15s backoff — deliberately the same shape as `scripts/pull-test-images.sh`, on the same
reading of ADR-0553: `apt-get`'s exit status carries no verdict-versus-transport ambiguity for
the retry to resolve, because a non-zero exit can only mean the packages are not installed. A
plain bounded retry is therefore correct and sufficient, and nothing here needs the
verdict classification `audit-deps.sh` carries.

**The budget is sized so that a timeout is expected to be survivable, not exceptional.** The
install ceiling defaults to 60s — several times the ~15s `libvirt-dev` costs, and an order of
magnitude under the wedge. That is deliberately tight enough to fire on a slow-but-working
mirror, which is safe precisely because the retry follows: it costs one attempt, not a red
check, and both recorded wedges recovered on a plain re-run. Sizing for the slowest legitimate
run instead would put the bound back above the wedge it exists to catch.

`apt-get update` fetches the repository indexes and costs the same whatever is installed
afterwards, so its ceiling is capped at 60s independently. Only the install budget follows the
package set, via `KDIVE_APT_TIMEOUT_S`, which `live.yml` raises to 300s for the ppc64le emulator
and the libguestfs appliance. The cap is what keeps that raise from tripling the worst case: 3 x
(60s + 300s) + 20s of backoff is about 18 minutes, inside that job's `timeout-minutes: 30`, so
the step still fails with its own diagnostic rather than being cut off by the job.

**Failure names the mirror and the attempt.** Every failed attempt logs the attempt number, which
of the two apt calls failed, whether it stalled or exited non-zero, the budgets in force, and the
mirror hosts apt is configured to use — read from `apt-get indextargets`, which parses local
configuration and never touches the network, so it is usable at exactly the moment the network is
not. The exhausted-budget line is an `::error::` annotation. Distinguishing a stall from a
non-zero exit is the part that matters: they have different causes and different fixes, and
#1978 was hard to diagnose because the log showed neither.

**Every job in a workflow that installs packages declares `timeout-minutes`.** Sized as the
job's observed runtime plus the script's own worst case plus margin, so the step fails with its
diagnostic before the job is cut off. `live.yml` already sized both of its jobs; the guard now
keeps that from being lost.

Per `AGENTS.md` the `justfile` is the single source of truth for commands, so the command text
lives in the script and `just apt-install <packages>` is the recipe. The workflows invoke the
script directly rather than through `just` — this step runs before `just` is set up in
`lint · type · test` and `test-ordering.yml`, and the two `supply chain` jobs never install
`just` at all. That is the same arrangement `audit-deps.sh` already uses, and it keeps the
command in one place either way; adding `setup-just` to two more jobs to shell out to a script
would add a network dependency to the very steps this record is bounding.

`tests/guards/test_apt_install_is_bounded.py` owns the wiring. Four of its tests are static: no
workflow calls `apt-get` directly, every package-installing workflow reaches the script, every
job in one declares `timeout-minutes`, and the retry shape still matches `pull-test-images.sh`.
The other three **run the script** against a stub `apt-get` that hangs, one that exits 100, and
one that succeeds, because no static assertion can tell whether the timeout actually fires — and
"no bare `apt-get` in the workflows" would pass just as happily over a script that hangs forever.

## Consequences

A wedged apt step now fails in about 6.5 minutes on the PR gate instead of running toward 360,
and it fails with a line naming the mirror rather than needing a human to recognize a hang. A
transient stall costs up to 20s of backoff and stays green.

The tight budget means a genuinely slow runner will sometimes retry where it previously
succeeded first time. That is the intended trade and it is visible in the log; if a mirror gets
slow enough to exhaust three attempts the step goes red, which is the correct outcome for an
install that is not going to finish.

Six call sites become one script, replacing six copies of a command that could drift. Adding a
system package to CI now means editing that script's caller, and the guard fails a workflow that
reintroduces a bare `apt-get`.

The script SIGKILLs `apt-get` by construction, which can leave dpkg mid-unpack. It therefore runs
`dpkg --configure -a` before each retry, non-fatally: that state is this script's own doing, so
recovering from it is not suppression, and a dpkg that is genuinely broken still fails the next
attempt and the exhausted-budget path.

`timeout-minutes` values are now a thing to keep roughly in step with observed runtimes. They are
deliberately loose — a backstop, not a performance budget — so ordinary growth will not trip
them, and a job that outgrows its value fails in a way that names the value.

This does not cover the other single-shot network calls ADR-0553 listed: `uv sync --locked`,
`go install`, the shellcheck tarball `curl`, `uvx prek`, `uvx zizmor`, and the
`build-push-action` base pulls. None has failed a run here. `apt-get` is separated from them by
having failed twice in one afternoon, not by being a different kind of call, and the pattern
here — bound, then retry — is what they should adopt if they start to.

## Considered & rejected

- **Retry on exit status alone, the ADR-0553 shape, and stop there.** The literal reading of
  #1978's second acceptance criterion and of the deferral in ADR-0553. Rejected on the evidence:
  `apt-get` never exited in either recorded occurrence, so this would have changed nothing about
  both failures it is meant to fix, while looking like a fix in the diff. The timeout is the
  load-bearing half.
- **Set `timeout-minutes` and stop there.** Bounds the damage and needs no script. Rejected: it
  converts a 33-minute wedge into a red check at whatever the job budget is, which still costs
  the CI cycle and still needs a human to re-run. It is kept as the backstop, not the fix.
- **`-o Acquire::http::Timeout=` and `Acquire::Retries=` alone.** apt's own knobs, no wrapper.
  Rejected as the whole answer: they bound one connection, not the call, so an apt that stalls
  anywhere outside a socket read — a mirror that trickles bytes, dpkg blocked on a lock — is
  untouched. They are set alongside the outer timeout, where they earn their keep by failing a
  blackholed mirror *inside* apt, whose error names the host and IP, rather than leaving the
  outer kill with nothing to read. `Acquire::Retries=0` is explicit for the opposite reason: this
  script owns the retry, and apt's own would multiply into a budget that does not show them.
- **`nick-fields/retry` or another remote retry action.** ADR-0553 rejected this for the image
  pull and the same reasons hold here: a new supply-chain entry and a new pin to keep truthful
  under ADR-0505, to solve what a shell loop solves. It also cannot express the part that
  matters — the hard timeout around each apt call.
- **One timeout for both `apt-get` calls.** Simpler: one constant, one env var, no cap. Rejected
  because `live.yml`'s 300s install budget would then apply to `update` as well, putting the
  worst case at about 30 minutes — outside that job's `timeout-minutes: 30`, so the job would be
  cut off before the step could report which mirror wedged it. The index fetch does not scale
  with the package set, so capping it separately costs one line.
- **A second env var for the update budget.** The general form of the cap above. Rejected as
  configuration with no caller: nothing needs to raise the index-fetch ceiling, and the only
  thing that would have exercised it is the guard test, which the `min` already covers by
  following a lowered budget down.
- **Add `setup-just` to the two `supply chain` jobs and call `just apt-install` uniformly.** The
  literal reading of "all call sites invoke the recipe". Rejected: it adds a networked action to
  two jobs in order to shell out to a script they can already run, in the same step this record
  exists to make less network-dependent. `audit-deps.sh` established the alternative — invoke the
  script, name the recipe in the comment — and the command text is in one place under both.
- **Assert only that no workflow contains a bare `apt-get install`.** What the issue's fourth
  acceptance criterion asks for literally, and cheap. Rejected as the whole guard: it is an
  emptiness assertion that passes over a repo with no workflows, over a glob that stopped
  matching, and — the case that matters — over a shared script that hangs forever. The static
  tests keep it, paired with a non-empty count of script invocations, and the behavioral tests
  carry the actual claim.
- **Prove the bound with a live unroutable mirror instead of a stub `apt-get`.** Closer to the
  reported failure. Rejected for the guard: it needs root, apt, and a way to blackhole a route,
  none of which a unit test on a developer machine has, and a test that skips itself on every
  developer machine is not a guard. The stub reproduces the property under test — a command that
  never returns — and the real script, the real `sudo` path and the real `timeout` are all still
  exercised.
