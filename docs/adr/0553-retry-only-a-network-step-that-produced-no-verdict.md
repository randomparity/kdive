# 0553 — Retry a network CI step only when it produced no verdict

## Status

Accepted (2026-08-07)

## Context

Two CI runs on #1912 failed on transient network errors unrelated to the diff. Each step
calls the network exactly once, so any blip fails the whole check.

The `supply chain (runtime)` job runs `pip-audit --no-deps --strict` against PyPI with a 15s
socket timeout, once per queried dependency. It failed with
`requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='pypi.org', port=443)`.

The `lint · type · test` job failed pulling `postgres:17` from Docker Hub
(`context deadline exceeded`). That one is worse than a single red check. The images are
pulled lazily by testcontainers from inside the suite, so an unreachable registry became
`3 failed, 8938 passed, 16 skipped, 3448 errors` — a reader has to scroll several screens to
find that one registry was down.

The obvious fix — wrap each step in a bounded retry loop — is **correct for the image pull and
wrong for `pip-audit`**, and the difference is what this ADR exists to record.

`pip-audit` exits 1 both when it finds a genuine advisory and when it cannot reach the
network. A loop that retries on exit status alone therefore re-runs a real finding until the
attempt budget is exhausted, and the only thing that distinguishes the two outcomes is how
long the job took. That is not merely a wasted retry: it is the shape of a change that
silently disarms a security gate, because the next reasonable-looking edit — "the last attempt
still failed, treat an exhausted budget as flaky and warn instead" — turns every advisory
green. The gate must not be one plausible refactor away from that.

Measured against `pip-audit@2.10.0`, the observable behavior is:

| outcome | exit | stdout under `-f json` |
|---|---|---|
| no advisories | 0 | valid JSON, every `vulns` empty |
| genuine advisory | 1 | **valid JSON**, some `vulns` non-empty |
| vulnerability service unreachable | 1 | **zero bytes** (traceback on stderr) |
| package index unreachable | 1 | **zero bytes** (traceback on stderr) |

The third row is the exact failure this issue reports. The signal that separates the rows is
not the exit code — it is whether the run **produced a verdict at all**.

## Decision

**Retry a network step only when it produced no verdict. Never retry a verdict.**

For `pip-audit`, "produced a verdict" means it emitted parseable JSON. `scripts/audit-deps.sh`
runs the audit with `-f json` to a file and classifies:

- **Parseable JSON** — the audit completed and its answer is authoritative. Not retried, ever.
  The script derives its own exit status from the *content*: non-empty `vulns` anywhere fails
  the runtime (gating) mode. The exit code of `pip-audit` is not consulted for this decision.
- **No parseable JSON** — the audit did not complete. Only this is retried, up to 3 attempts
  with 5s/15s backoff.
- **Attempts exhausted** — fails. An unreachable PyPI is an unaudited dependency set, and an
  unaudited set is not a passing audit. The gate stays fail-closed on every path; the only
  route to exit 0 is a completed audit that found nothing.

The audit also stops running under `uvx`. `pip-audit` resolves wheels for the interpreter it
runs on, and an ephemeral `uvx` environment is built on whatever Python `uv` happens to find.
On an interpreter other than the project's it selects a different ABI's wheels and dies on
`THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE` — hashes a lock pinned to
`requires-python = "==3.14.*"` never contained. Measured here: `uvx pip-audit@2.10.0`, the
command `ci.yml` runs today, picks a 3.13 interpreter, selects
`psycopg_binary-…-cp313-…whl`, and fails; the same audit under `uv run --with` exits 0. That
is a red supply-chain gate with no advisory behind it, and it is invisible on a runner whose
default interpreter happens to match — which is why CI is green on it today. The script uses
`uv run --with 'pip-audit==2.10.0'`, which takes the project's interpreter by construction, so
there is no second thing to keep in step.

`--strict` stays on. A dependency-collection failure is a distinct condition from a query
failure and this change does not soften it: when `--strict` aborts collection, `pip-audit`
also emits no JSON, so the script retries it — which is right for a collection failure caused
by a network fetch, and merely costs two wasted attempts before failing when it is caused by a
missing header. Either way it cannot pass.

For the images, the exit status of `docker pull` carries no comparable ambiguity — there is no
"finding" for it to be confused with — so a plain bounded retry is correct and sufficient.
`scripts/pull-test-images.sh` pulls `postgres:17` and the pinned MinIO tag ahead of the suite,
3 attempts with the same backoff, and is wired as its own step before `just test` in **both**
`ci.yml` and `test-ordering.yml`. Both run `just test` with `KDIVE_REQUIRE_DOCKER=1`, so both
carry the cascade; fixing only the PR gate would leave the weekly ordering run failing in the
3448-error shape this issue is about.

Pre-pulling changes what a registry outage looks like, not whether it fails: the step goes red
by itself, in seconds, with the failing image name. It does not make the suite tolerate a
missing image, and it deliberately does not warm a cache for correctness — the fixtures still
pull lazily if the step were removed.

**The tags are duplicated, and a guard owns the duplication.**
`tests/guards/test_prepull_images_match_fixtures.py` parses `_POSTGRES_IMAGE` out of
`tests/db/conftest.py` and `_MINIO_IMAGE` out of `tests/store/conftest.py` by name, parses the
tags out of the pull script, and asserts the two sets are equal and non-empty. It also asserts
both workflows invoke the pre-pull recipe *before* their `just test` step. It is read-only over
all four files: it parses text and never imports the fixtures, so it needs no Docker, no
network, and no project install.

Per `AGENTS.md` the `justfile` is the single source of truth for commands, so the tags live in
the script a `just` recipe invokes, and the workflows call `just pull-test-images` — not a
re-typed `docker pull`. CI invokes recipes individually, so the step is listed explicitly in
both workflows rather than added to the `ci:` aggregate.

## Consequences

A transient PyPI or Docker Hub failure now costs up to ~20s of backoff instead of a red check
and a manual re-run. A sustained outage still fails, one step, quickly.

`scripts/audit-deps.sh` becomes the single implementation for three call sites — `just audit`,
and both `supply chain` jobs — replacing three separately drifting command strings. The dev
mirror keeps its informational, never-fatal behavior, and gains the same classification, so a
timeout there no longer posts a job-summary warning that reads like a discovered advisory.

The runtime gate's pass/fail now comes from parsing `pip-audit`'s JSON rather than from its
exit status. That is a deliberate reduction in what the gate trusts upstream to encode, and it
is one more thing a `pip-audit` bump could break — the version stays pinned at `2.10.0`, now
in one place, and a renamed or dropped `dependencies` key reads as "no verdict", so such a
bump fails the gate loudly instead of greening it.

Both `supply chain` jobs now resolve the project environment, where before they ran
`pip-audit` standalone. They already apt-install `libvirt-dev` for exactly this reason, so the
toolchain is present; the cost is the sync, and it buys the interpreter match above.

Adding a third test-fixture image, or bumping either tag, now requires editing the pull script
in the same change. The guard names both files and the mismatched values, so the fix is
mechanical.

The pre-pull step adds roughly the pull time to every `lint · type · test` run. It is not new
work — the suite pulled the same layers — it moves earlier and is attributable.

## Considered & rejected

- **Wrap both steps in `nick-fields/retry`.** The issue suggests it. Rejected: it adds a
  remote-action dependency (a new supply-chain entry, a new pin to keep truthful under
  ADR-0505) to solve a problem a five-line shell loop solves, and — decisively — it retries on
  exit status, which is exactly the classification error above. It would re-run genuine
  advisories and could not be made not to.
- **Retry `pip-audit` on exit status, bounded, and fail if the last attempt fails.** The naive
  form, and it does stay fail-closed *today*. Rejected because it is fail-closed by accident
  rather than by construction: it cannot tell a finding from a timeout, so it wastes the full
  budget on every real advisory, and it leaves the gate one plausible "exhausted budget means
  flaky" edit away from passing them. Encoding the distinction is the whole point.
- **Grep `pip-audit`'s human-readable output for `Found N known vulnerabilities`.** Also
  distinguishes the cases, without `-f json`. Rejected: it couples a security gate to an
  unversioned English string, and it fails open in the direction that matters — a reworded
  message reads as "no findings".
- **Keep `uvx` and pin the interpreter (`uvx --python 3.14 …`).** Verified to work, and it
  avoids the project sync. Rejected because it restates `requires-python` in a second place
  with nothing keeping the two in step — the next Python bump would reintroduce the same
  hash failure, in a form that again looks like a supply-chain finding.
- **Raise `--timeout` and stop there.** The reported failure was a *read* timeout at 15s;
  a larger value makes it rarer, not absent, and does nothing for the Docker Hub 500 or for
  the cascade. Worth doing alongside, not instead — and it is not done here, because the
  retry makes the timeout's exact value uninteresting.
- **Pull the images from inside the fixtures instead, with retry there.** It would fix the
  cascade at its source and cover a developer's laptop too. Rejected here for two reasons:
  `tests/db/conftest.py` and `tests/store/conftest.py` are owned by concurrent work on #1911
  and off-limits to this change, and a retry inside the fixture still reports as thousands of
  errors when it finally gives up, because the failure is still inside the suite. The
  dedicated step is what makes the outage one line.
- **Put the tags in the workflow YAML and guard that.** The literal reading of the issue's
  suggestion. Rejected because it puts a command in two workflow files instead of in the
  `justfile`, against `AGENTS.md`'s single-source-of-truth rule, and it would give a developer
  no way to run the pre-pull locally. The guard covers the same drift either way.
- **Refactor the two tags into one shared constant both conftests import.** The way to
  eliminate the duplication rather than guard it, and the better end state. Not done here:
  both conftests are being edited by concurrent work on #1911, and a guard that reads them is
  compatible with that refactor landing later — the guard parses by name and would keep
  working, or fail loudly and mechanically, once a shared constant replaces the literals.
- **Also retry the remaining single-shot network calls** — `apt-get`, `uv sync --locked`,
  `go install`, the shellcheck tarball `curl`, `uvx prek`, `uvx zizmor`, and the
  `build-push-action` base pulls. Each has the same exposure. Deliberately left out: none of
  them has failed a run here, they are a uniform mechanical change better made as one sweep,
  and none carries the verdict-versus-transport ambiguity that made this change worth an ADR.
  Tracked separately.
