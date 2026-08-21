# 0566 — Bound a CI apt install with a hard timeout, then retry it

## Status

Accepted (2026-08-17)

> **Superseded by [0571](0571-every-job-in-every-workflow-declares-a-timeout.md)** (2026-08-21)

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

**`DPkg::Use-Pty=0` is not a detail; without it the timeout is decorative.** `timeout` signals
its own process group, and apt's default pty mode starts `dpkg` in a *new* group and a new
session outside it — measured in `ubuntu:24.04` as `timeout` pgid 141, `apt-get` pgid 141,
`dpkg` pgid 321 sid 321 — where neither the SIGTERM nor the `--kill-after` SIGKILL can reach it. `dpkg` also ignores SIGHUP
(`SigIgn` 0x7), so the pty hangup does not end it either. A budget that expired mid-unpack
therefore left an orphaned root `dpkg` holding `/var/lib/dpkg/lock`, and because apt's compiled
`DPkg::Lock::Timeout` default is 0 — fail immediately — every remaining attempt died instantly
against that lock. The script would have reported an exhausted budget and blamed the mirror,
having damaged the runner itself. `DPkg::Use-Pty=0` keeps `dpkg` in apt's group where the kill
lands, and `DPkg::Lock::Timeout=30` makes a retry wait out a still-exiting `dpkg` rather than
burn an attempt on it. The only thing given up is apt's pty progress rendering, which no CI log
reads.

The same reasoning makes the repair mandatory rather than optional: a killed install leaves
packages half unpacked, so `dpkg --configure -a` runs after **every** failed attempt including
the last, itself under `timeout` because it executes maintainer scripts. Skipping it on the
exhaustion path would hand a developer who ran `just apt-install` a broken package database with
nothing in the log to say so.

The repair is skipped after a failed `update`, which unpacked nothing — repairing there would
only assert a database problem that does not exist.

`KDIVE_APT_TIMEOUT_S` is validated as a positive whole number. `timeout 0s` means *no limit*, so
an unvalidated budget would let one mistyped digit silently restore the unbounded behavior of
#1978 while the log went on printing a budget as though it were enforcing one.

**Not every stall is a mirror.** A debconf question or a conffile prompt stalls apt just as
effectively and is *not* transient, so it would burn all three attempts identically and go red —
the one failure shape this design's retry cannot absorb. `DEBIAN_FRONTEND=noninteractive` (passed
through `sudo env`, because sudoers may refuse to forward an environment variable) and
`Dpkg::Options::=--force-confold` close both halves. Keeping the installed conffile is what an
unattended `-y` install already means to mean.

**The script refuses to run off Debian/Ubuntu.** `just apt-install` is the documented developer
entry point and this project's own dev hosts are Fedora, where the loop would otherwise spend 20s
of backoff re-running a command that cannot exist and close by advising a `dpkg` repair on a
machine with no dpkg. Three identical `127`s are not a transient failure.

**The budget is sized so that a timeout is expected to be survivable, not exceptional.** The
install ceiling defaults to 60s — several times the ~15s `libvirt-dev` costs, and an order of
magnitude under the wedge. That is deliberately tight enough to fire on a slow-but-working
mirror, which is safe precisely because the retry follows: it costs one attempt, not a red
check, and both recorded wedges recovered on a plain re-run. Sizing for the slowest legitimate
run instead would put the bound back above the wedge it exists to catch.

`apt-get update` fetches the repository indexes and costs the same whatever is installed
afterwards, so its ceiling is capped at 60s independently. Only the install budget follows the
package set, via `KDIVE_APT_TIMEOUT_S`, which `live.yml` raises to 180s for the ppc64le emulator
and the libguestfs appliance — measured at ~30s for that whole set, so six times the observed
cost.

The worst case for a total outage is about 11 minutes at the defaults: three attempts of two
bounded calls, the SIGKILL grace on each, a bounded repair after each attempt, and 20s of
backoff. `live.yml` reaches about 17 minutes on its larger budget, which is why that job's
`timeout-minutes` had to grow from 30 to 50: 30 was sized for the emulated boot alone, and
leaving it there would have let a mirror outage cut the job off before the step could name the
mirror. Every other job-level value is the job's observed runtime plus the ~11 minutes plus
margin.

**Failure names the phase, the attempt, and the configured mirrors.** Every failed attempt logs
the attempt number, which of the two apt calls failed, whether it stalled or exited non-zero, the
budgets in force, and the mirror hosts apt is configured to use — read from `apt-get
indextargets`, which parses local configuration and never touches the network, so it is usable at
exactly the moment the network is not, and bounded like every other apt call here. The
exhausted-budget line is an `::error::` annotation.

The mirror list is labelled *configured* rather than *failing*, deliberately. On a hosted runner
it is a constant, so on its own it discriminates nothing; what discriminates is the phase. A
stall in `update` is a download stall. A stall in `install` may equally be local unpack work
overrunning the budget, and the `::error::` line says so rather than implying the network. That
distinction is the part that matters — a stall and a non-zero exit have different causes and
different fixes, and #1978 was hard to diagnose because the log showed neither.

**Every job in a workflow that installs packages declares `timeout-minutes`.** Sized as the
job's observed runtime plus the script's own worst case plus margin, so the step fails with its
diagnostic before the job is cut off. `live.yml` had already sized both of its jobs, but for
their own work only — its `tcg` job is raised here because this change adds a new worst case to
it, not because 30 was wrong for what it previously covered.

Per `AGENTS.md` the `justfile` is the single source of truth for commands, so the command text
lives in the script and `just apt-install <packages>` is the recipe. The workflows invoke the
script directly rather than through `just` — this step runs before `just` is set up in
`lint · type · test` and `test-ordering.yml`, and the two `supply chain` jobs never install
`just` at all. That is the same arrangement `audit-deps.sh` already uses, and it keeps the
command in one place either way; adding `setup-just` to two more jobs to shell out to a script
would add a network dependency to the very steps this record is bounding.

`tests/guards/test_apt_install_is_bounded.py` owns the wiring. Four of its tests are static: no
workflow calls `apt-get` directly — matched anywhere in a `run:` value, because anchoring the
pattern at the start of a line catches the block-scalar form and lets the `run: sudo apt-get
install …` one-liner straight through — every package-installing workflow reaches the script,
every job in one declares `timeout-minutes`, and the retry shape still matches
`pull-test-images.sh`.

The other six **run the script**: against a stub `apt-get` that hangs on `update`, one that
exits 100, one that succeeds, one that lets `update` pass and hangs on `install`, and one that
rejects a malformed budget. No static assertion can tell whether the timeout actually fires, and
"no bare `apt-get` in the workflows" would pass just as happily over a script that hangs forever.

The install-hang stub earns its place by measurement. Without it, every failure stub failed on
the *first* apt call — `update` — so the `install` call, the one the issue names, was never
exercised on a failure path, and deleting its `timeout` left the whole suite green. The runs also
record the argv every stub was handed and match each call as one bounded command, because
membership tests on `sudo`, `--kill-after` and `install` as separate substrings are all satisfied
by a script that bounds `update` and leaves `install` bare. Twelve mutations were run against the
result — an unbounded install, an unbounded or unprivileged repair, a repair skipped on the last
attempt, and each of the six `-o` options deleted individually — and all twelve fail the suite.

## Consequences

A wedged apt step now fails in about 11 minutes on the PR gate instead of running toward 360,
and it fails with a line naming the phase, the attempt and the configured mirrors rather than
needing a human to recognize a hang. A transient stall costs up to 20s of backoff and stays
green.

The tight budget means a genuinely slow runner will sometimes retry where it previously
succeeded first time. That is the intended trade and it is visible in the log; if a mirror gets
slow enough to exhaust three attempts the step goes red, which is the correct outcome for an
install that is not going to finish.

Six call sites become one script, replacing six copies of a command that could drift. Adding a
system package to CI now means editing that script's caller, and the guard fails a workflow that
reintroduces a bare `apt-get`.

The script SIGKILLs `apt-get` by construction, which can leave dpkg mid-unpack. It therefore runs
a bounded `dpkg --configure -a` after every failed attempt, non-fatally: that state is this
script's own doing, so recovering from it is not suppression, and a dpkg that is genuinely broken
still fails the next attempt and the exhausted-budget path. Verified end to end in
`ubuntu:24.04`: a 20s budget against the `live.yml` package set kills attempt 1 mid-unpack,
leaves no orphaned `dpkg` and no held lock, and attempt 2 completes the install.

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
- **Leave apt's own `Acquire::Retries` on.** Rejected, with a cost worth naming: apt re-fetches
  an *individual file* in about a second, where this script re-runs the whole attempt. Turning it
  off trades that fine granularity for a budget that is legible from outside the process — apt's
  retries would otherwise consume the outer timeout without appearing in it. One flaky file that
  apt would have absorbed invisibly now costs one attempt out of three, which the backoff and the
  remaining attempts absorb.
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
