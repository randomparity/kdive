# Plan: stack entry points named for the state they leave

**Goal.** Rename the live-stack entry points so each says where it stops, and give every layer
below project funding one implementation with one backend readiness contract.

**Architecture.** `scripts/live-stack/stack-services.sh` becomes the single bring-up script,
taking `--stage backends|services`. `just stack-backends` is a thin recipe over its `backends`
stage. `scripts/live-stack/lib.sh` keeps the sourced variables and gains the backend bring-up
function. The demo wrapper under `examples/local-libvirt/` still composes bring-up, onboarding,
and client wiring, under names that say so.

**Tech stack.** Bash (`set -euo pipefail`, shellcheck + `shfmt -i 2`), `just`, Docker Compose
v2, Python 3.14 tests under pytest.

Spec: [`2026-09-15-stack-entry-points-design.md`](../specs/2026-09-15-stack-entry-points-design.md).
Decision: [ADR-0655](../../adr/0655-stack-entry-points-named-for-end-state.md).

Expected implementation size: 420–520 changed lines (L) — 212 matching lines across 38
live-surface files (`rg -n --hidden '\b(up|down|status)\.sh\b|\bstack-up\b' --glob '!.git/**'
--glob '!CHANGELOG.md' | grep -vE '^docs/(adr|debt|archive|superpowers|design|workflow)/' |
wc -l`, measured 2026-09-15), plus Task 2's readiness function, stage dispatch, and new test
cases, less the deleted poll and recipe body.

## Global Constraints

- Python `==3.14.*`; ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict.
- Shell: `set -euo pipefail`, `shfmt -i 2 -d` clean, shellcheck clean (`.shellcheckrc` applies).
- `scripts/live-stack/lib.sh` is **sourced, never executed** (`lib.sh:2-4`): it may define
  variables and functions and must have no other side effects.
- Prose rule: "Milestone", never "Sprint"; avoid "critical", "robust", "comprehensive",
  "elegant" in code comments, docs and commit messages.
- Never sweep a record: `docs/adr/`, `docs/debt/`, `docs/archive/`, `docs/superpowers/`,
  `docs/design/` proof records, merged `docs/workflow/` plans. No gate enforces this —
  `check-records.sh` checks required fields and non-disappearance, and permits edits — so it
  holds by intent.
- Gates: `just lint`, `just type`, `just ci`. Run `just ci` bare, redirected, never piped:
  `just ci > /tmp/ci.log 2>&1 < /dev/null`.
- Branch: `refactor/stack-entry-points`. Base: `main`.

## Task 1 — Rename the entry points and sweep every live reference

Rename plus the reference updates that keep the tree working — no behavior change, so a
reviewer can check it by reading names. It is bisect-clean only if every reference form is
covered, which is why Step 1.4 has three passes and the acceptance criteria run the tests.

**Interfaces.** Produces the paths Task 2 edits:
`scripts/live-stack/stack-services.sh`, `scripts/live-stack/stack-down.sh`,
`scripts/live-stack/stack-status.sh`, `examples/local-libvirt/demo-up.sh`,
`examples/local-libvirt/demo-down.sh`, and the justfile recipe `stack-backends`. Consumes
nothing from earlier tasks.

### Step 1.1 — Generate and classify the file map

```sh
cd "$(git rev-parse --show-toplevel)"
rg -l --hidden '\b(up|down|status)\.sh\b|\bstack-up\b' \
  --glob '!.git/**' --glob '!CHANGELOG.md' > /tmp/entrypoint-refs.txt
wc -l /tmp/entrypoint-refs.txt
```

Expect 73 paths as measured on 2026-09-15 — 35 under the record roots, 38 live surface. Treat
a different total as drift to re-classify, not as an error. Split them: every path under `docs/adr/`, `docs/debt/`,
`docs/archive/`, `docs/superpowers/`, `docs/design/`, or `docs/workflow/` is **not swept**;
everything else is live surface and gets updated in this task. Keep both lists — Step 1.5
asserts the live list is empty afterwards.

### Step 1.2 — Rename the five scripts

```sh
git mv scripts/live-stack/up.sh     scripts/live-stack/stack-services.sh
git mv scripts/live-stack/down.sh   scripts/live-stack/stack-down.sh
git mv scripts/live-stack/status.sh scripts/live-stack/stack-status.sh
git mv examples/local-libvirt/up.sh   examples/local-libvirt/demo-up.sh
git mv examples/local-libvirt/down.sh examples/local-libvirt/demo-down.sh
```

### Step 1.3 — Rename the justfile recipe

In `justfile`, rename the `stack-up:` recipe to `stack-backends:` and leave its body alone for
now (Task 2 replaces it). Update its doc comment to say what it leaves behind:

```just
# Bring up the compose backends, create and verify the artifacts bucket, and migrate the
# schema. This is NOT a running stack: scripts/live-stack/stack-services.sh starts libvirt and
# the host processes, and `just onboard` funds a project. See docs/operating/runbooks/live-stack.md.
stack-backends:
```

### Step 1.4 — Sweep every live reference

For each live-surface path from Step 1.1, replace old names with new. The substitutions, in
this order (longest first, so a path-qualified match is not half-rewritten):

| Old | New |
|---|---|
| `scripts/live-stack/up.sh` | `scripts/live-stack/stack-services.sh` |
| `scripts/live-stack/down.sh` | `scripts/live-stack/stack-down.sh` |
| `scripts/live-stack/status.sh` | `scripts/live-stack/stack-status.sh` |
| `examples/local-libvirt/up.sh` | `examples/local-libvirt/demo-up.sh` |
| `examples/local-libvirt/down.sh` | `examples/local-libvirt/demo-down.sh` |
| `just stack-up` | `just stack-backends` |

A path-qualified substitution alone leaves the tree broken. Three further forms must be
handled, each verified present on 2026-09-15:

**Sibling invocations inside the renamed script.** `up.sh:47` runs `"${here}/down.sh" --wipe
--yes` and `up.sh:226` runs `"${here}/status.sh"`. The second is unconditional, so every
bring-up would die at the status phase; the first fires on `--reset-db`, which
`.github/workflows/live.yml:413` passes. Update both to the new basenames.

**Bare basenames built from parts.** `tests/scripts/test_live_stack_scripts.py` (2624 lines,
45 matches) is Task 1's largest consumer and builds paths from a tuple at `:876-882`
containing `"up.sh"`, `"down.sh"`, `"status.sh"` — no path-qualified literal exists to
substitute, and the reads raise `FileNotFoundError` after Step 1.2.

**The recipe name as a bare argument.** `tests/scripts/test_live_stack_scripts.py:2591` passes
`"stack-up"` as a `just` argument. It contains no `just stack-up` literal, so neither the
substitution table nor the Step 1.5 guard sees it; three tests reach it via `_run_stack_up` and
fail with `Justfile does not contain recipe 'stack-up'`. Those tests exercise the recipe body
Task 2 replaces — rename the token here, and let Task 2 update what they assert.

Then re-read every touched file and fix the remaining bare references:
a comment saying "up.sh" with no path (`scripts/live-vm/preflight-env.sh:85`,
`deploy/ansible/inventory/group_vars/live_vm_runners.yml:11`,
`tests/integration/live_stack/spine.py:71`, `examples/local-libvirt/env.sh`,
`examples/local-libvirt/build-image.sh`), and `scripts/live-stack/README.md`, whose section
headings name the scripts.

Three call sites in `.github/workflows/live.yml` (lines 413, 527, 770) invoke the script
directly; its comment at 523-524 also names `just stack-up` and stays accurate after the
rename. `deploy/ansible/roles/live_vm_host/{tasks,defaults}/main.yml` reference the path in
variables and comments.

`src/kdive/config/external_env.py` carries a `KDIVE_*` description naming the script; after
editing it, regenerate the committed reference:

```sh
just config-docs
```

Expect `docs/guide/reference/config.md` to change. `just config-docs-check` then passes.

While in `AGENTS.md`, fix the guidance the rename exposes as wrong: the bring-up table at
`AGENTS.md:123-124` instructs `just stack-up` **then** the bring-up script, which is redundant
because the script brings the backends up itself, and which CI contradicts at
`live.yml:523-524`. Both rows become `scripts/live-stack/stack-services.sh` alone. Do the same
in `docs/operating/runbooks/live-stack.md` (§1 and §4 are not sequential prerequisites) and
`scripts/live-stack/README.md:47` (`stack-backends` does not serve `just test-live-stack`,
which needs host processes).

### Step 1.5 — Add the sweep guards

In `tests/scripts/test_live_workflow_shape.py`:

```python
# The regex that generated the file map, so the guard's reach equals the problem's:
# path-qualified names AND bare basenames. A literal list would miss the latter.
#
# The negative lookbehind is load-bearing. Every replacement name embeds its predecessor
# (`stack-down.sh`, `demo-up.sh`), and `-` is a word boundary, so the map-generating
# `\bdown\.sh\b` matches inside the new name and the guard can never go green.
_OLD_ENTRY_POINT_RE = re.compile(r"(?<![-\w/])(?:up|down|status)\.sh\b|(?<![-\w])stack-up\b")
# This module necessarily contains the pattern it searches for, so it excludes itself.
_SELF = "tests/scripts/test_live_workflow_shape.py"
# Append-only records (the `records` gate) and point-in-time records keep citing the old
# names on purpose: ADR-0655 is where a reader learns the current ones.
_RECORD_ROOTS = (
    "docs/adr/",
    "docs/debt/",
    "docs/archive/",
    "docs/superpowers/",
    "docs/design/",
    "docs/workflow/",
)


def _tracked_live_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [p for p in out if p not in ("CHANGELOG.md", _SELF) and not p.startswith(_RECORD_ROOTS)]


def test_no_live_file_names_a_renamed_entry_point() -> None:
    offenders: list[str] = []
    for path in _tracked_live_files():
        full = _ROOT / path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _OLD_ENTRY_POINT_RE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, "renamed entry points still referenced:\n" + "\n".join(offenders)


def test_workflow_script_paths_exist() -> None:
    workflow = _LIVE.read_text(encoding="utf-8")
    for match in re.findall(r"scripts/live-stack/[\w.-]+\.sh", workflow):
        assert (_ROOT / match).is_file(), f"live.yml names a missing script: {match}"
```

The module already defines `_ROOT` (line 18, `pathlib.Path(__file__).resolve().parents[2]`)
and `_LIVE` (line 19, the `live.yml` path), and already imports `pathlib`, `re` and
`subprocess`. Use those; do not add a second root constant.

Confirm red before the sweep by stashing Step 1.4, then green after:

```sh
uv run python -m pytest tests/scripts/test_live_workflow_shape.py -q
```

Expect `2 passed` among that module's cases.

### Step 1.6 — Retarget the app-tier guard

`tests/live_stack/test_up_invariants.py` opens exactly one file — `_UP` at `:12` — and Task 2
moves the backend `docker compose up` into `lib.sh`. The guard must therefore read **both**
bring-up files, or the invariant it protects stops being covered where the code now lives.

Reading `lib.sh` is what makes a comment skip necessary: `lib.sh:229` is a comment
(`# Assumes env.sh is sourced and compose backends are up. KDIVE_WORKER_COUNT stays within
1..8.`) that matches `compose\b.*\bup\b` and contains `WORKER`, so a case-insensitive check
fails on it. Skip comments. Do **not** skip the observability line: it carries no app-tier name
today, so it passes anyway, and skipping it would let
`docker compose --profile obs up -d server` through a guard that catches it now.

```python
_LIVE_STACK = _REPO_ROOT / "scripts" / "live-stack"
_SERVICES = _LIVE_STACK / "stack-services.sh"
_LIB = _LIVE_STACK / "lib.sh"
_BRING_UP_FILES = (_SERVICES, _LIB)
_APP_TIER = ("migrate", "server", "worker", "reconciler")
_COMPOSE_UP = re.compile(r"compose\b.*\bup\b")


def _compose_up_lines(text: str) -> list[str]:
    """Non-comment `compose ... up` lines.

    Comments are skipped because a comment can mention `compose ... up` without running
    anything; the observability line is deliberately NOT skipped, so a future
    `--profile obs up -d server` still fails this guard.
    """
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _COMPOSE_UP.search(line):
            lines.append(line)
    return lines


@pytest.mark.parametrize("path", _BRING_UP_FILES, ids=lambda p: p.name)
def test_bring_up_never_starts_the_app_tier(path: Path) -> None:
    for line in _compose_up_lines(path.read_text()):
        for svc in _APP_TIER:
            assert not re.search(rf"\b{svc}\b", line, re.IGNORECASE), (
                f"{path.name} starts app-tier service in: {line!r}"
            )
```

`re.IGNORECASE` is on its own merits: a guard whose verdict depends on letter case is not a
guard. Add `import pytest` and `from pathlib import Path` if absent.

Point `test_up_reconciles_app_tier_before_start` at `_SERVICES`. Leave
`test_up_uses_the_canonical_backend_list` reading `_SERVICES` for now — the rename has not
changed which services the script names — and note that Task 2 Step 2.5 replaces it, because
the script stops naming `KDIVE_BACKEND_SERVICES` once the array moves behind
`live_stack_backends_up`.

### Verification

- **Every live reference updated** — Mode: `focused-test`. Contract: Success criterion 2.
  `test_no_live_file_names_a_renamed_entry_point`. Red before Step 1.4 (the sweep), green
  after. `uv run python -m pytest tests/scripts/test_live_workflow_shape.py -q`.
- **CI call sites resolve** — Mode: `focused-test`. Contract: `live.yml` names existing paths.
  `test_workflow_script_paths_exist`. Red if a rename misses a call site.
- **App-tier guard still bites after the rename** — Mode: `focused-test`. Contract: the
  bring-up script never starts the compose app tier. `test_services_never_starts_the_app_tier`.
  Red while `_SERVICES` points at the deleted `up.sh` (the read raises). Confirm the predicate
  bites by temporarily adding `docker compose up -d server` to the script and observing the
  failure, then reverting.
- **Generated config reference** — Mode: `focused-test`. `just config-docs-check`. Red between
  editing `external_env.py` and running `just config-docs`.

### Acceptance criteria

1. `git ls-files` shows the five new script names and none of the five old ones.
2. `rg -l 'just stack-up|live-stack/(up|down|status)\.sh'` matches only files under the record
   roots.
3. `just lint`, `just lint-shell`, `just lint-workflows`, `just config-docs-check` pass.
4. `uv run python -m pytest tests/scripts tests/live_stack tests/integration/live_stack -q`
   passes. Task 1 is the commit that breaks these if a reference form was missed, so it is the
   commit that must run them — `just test` is not reached until Task 2.
5. No behavior change: `git diff` on the renamed scripts shows only name and comment edits.

## Task 2 — One staged bring-up with one readiness contract

Behavior change, isolated from the rename so a bisect can separate them.

**Interfaces.** Consumes the paths Task 1 produced. Defines, in `scripts/live-stack/lib.sh`:

```sh
# live_stack_backends_up  — no arguments; requires `docker` on PATH and the repo root as cwd.
# Returns 0 when the three long-running backends are healthy and the artifacts bucket exists
# and is versioned; non-zero otherwise, propagating the one-shot's status.
live_stack_backends_up() { ... }
```

`KDIVE_BACKEND_SERVICES` keeps its current definition and meaning (all four services, consumed
by `stack-status.sh`). The new `KDIVE_BACKEND_LONG_RUNNING` names only the three that `--wait`
may cover.

### Step 2.1 — Add the shared backend function to `lib.sh`

After the existing `KDIVE_BACKEND_SERVICES` definition:

```sh
# The subset `--wait` may cover. `docker compose up --wait` treats ANY container exit as a
# wait failure, so including the run-to-completion seaweedfs-init here would make a healthy
# stack report failure. The one-shot runs separately, below, where its exit status propagates.
# shellcheck disable=SC2034 # consumed by sourcing scripts
KDIVE_BACKEND_LONG_RUNNING=(postgres seaweedfs oidc)

# Bring the compose backends up and prove the artifacts bucket. Sourced-only (this file runs
# nothing at source time); callers invoke it explicitly.
live_stack_backends_up() {
  # When KDIVE_OIDC_IMAGE is unset the oidc service builds from ./deploy/mock-oidc (ADR-0357).
  # Pre-build it so the following `up` finds kdive-mock-oidc:dev locally instead of attempting
  # a doomed pull against a local-only tag, which prints a "pull access denied" warning that
  # reads as a hard failure. Skip when the image exists: its inputs change rarely and
  # `compose build` re-contacts the registry on every call even when fully cached. The skip is
  # announced so an operator editing deploy/mock-oidc knows to remove the tag to force one.
  if [[ -z "${KDIVE_OIDC_IMAGE:-}" ]]; then
    if docker image inspect kdive-mock-oidc:dev >/dev/null 2>&1; then
      echo "using cached kdive-mock-oidc:dev — run 'docker rmi kdive-mock-oidc:dev' to force a rebuild after editing deploy/mock-oidc" >&2
    else
      docker compose build oidc
    fi
  fi

  # --wait-timeout is required because the backends carry `restart: on-failure`
  # (docker-compose.yml): a container that keeps failing cycles Exited -> Restarting instead of
  # settling, so without a bound the convergence poll can block indefinitely rather than
  # reporting. The `stack-up` recipe cited ADR-0449 here; that record is "Warm the runtime pool
  # at process start" and governs systemd supervision, not compose, so the citation is dropped
  # rather than carried into a new file.
  # Two of the three deployments are non-interactive CI actors that cannot run `docker compose
  # ps` themselves, and --wait's timeout names no service — where the postgres poll this
  # replaces named its own. Dump the table so the job log carries the same signal.
  if ! docker compose up -d --wait --wait-timeout 120 "${KDIVE_BACKEND_LONG_RUNNING[@]}"; then
    docker compose ps >&2
    return 1
  fi

  # Creates the bucket, enables versioning, verifies Enabled, then exits. `run --rm` so a
  # failure here fails bring-up: the replaced `up -d` form never surfaced this exit status,
  # and a missing bucket then surfaced much later as a worker store check (ADR-0655).
  docker compose run --rm seaweedfs-init
}
```

### Step 2.2 — Add `--stage` to `stack-services.sh`

`up.sh:27` iterates `for arg in "$@"`, whose word list is expanded once before the body runs,
so a `shift` inside it cannot consume a value token: `--stage backends` would set the stage on
iteration 1 and then fall to the catch-all on iteration 2 with
`unknown argument: backends`. Converting the loop is therefore part of this step, not an
incidental edit. Replace `up.sh:27-37` entirely:

```sh
stage="services"
while [[ $# -gt 0 ]]; do
  case "$1" in
  --reset-db) reset_db=1 ;;
  --skip-obs) skip_obs=1 ;;
  --skip-libvirt) skip_libvirt=1 ;;
  --stage)
    shift
    stage="${1:-}"
    case "$stage" in
    backends | services) ;;
    *)
      echo "unknown --stage '${stage}': expected 'backends' or 'services'" >&2
      exit 2
      ;;
    esac
    ;;
  *)
    echo "unknown argument: $1 (accepts --stage, --reset-db, --skip-obs, --skip-libvirt)" >&2
    exit 2
    ;;
  esac
  shift
done
```

The existing flag variables are `reset_db`, `skip_obs` and `skip_libvirt`, declared at
`up.sh:24-26`; `skip_obs` defaults from `KDIVE_SKIP_OBS`. Reject only the two flags whose
phases the `backends` stage cannot reach:

```sh
stage="services"
# ... inside the existing argument loop:
  --stage)
    shift
    stage="${1:-}"
    case "$stage" in
      backends | services) ;;
      *)
        echo "unknown --stage '${stage}': expected 'backends' or 'services'" >&2
        exit 2
        ;;
    esac
    ;;
```

After parsing:

```sh
if [[ "$stage" == "backends" ]]; then
  for flag in reset_db skip_libvirt; do
    if [[ "${!flag}" == "1" ]]; then
      echo "--stage backends does not reach the phase --${flag//_/-} controls" >&2
      exit 2
    fi
  done
fi
```

`skip_obs` is deliberately absent from that loop. It defaults from the documented
`KDIVE_SKIP_OBS` environment variable (`up.sh:25`; `src/kdive/config/external_env.py`;
`examples/local-libvirt/demo-up.sh` sets it to `1`), so rejecting it would fail
`KDIVE_SKIP_OBS=1 just stack-backends` over a flag the operator never passed. The `backends`
stage does not reach the obs phase, so the value is simply inert there.

### Step 2.3 — Replace the inline backend block with the shared function

Delete the oidc pre-build block, the `docker compose up -d "${KDIVE_BACKEND_SERVICES[@]}"`
line, and the 30-iteration postgres health loop. In their place:

```sh
banner "backends"
live_stack_backends_up
```

Two phases that sit inside or beside the old backends block belong to `services`, not
`backends`, and must be gated explicitly — the plan's earlier draft left both running under
`--stage backends`, which would have made the new name wrong on its first use.

**The app-tier reconcile** (`up.sh:60-63`, `docker compose rm -sf migrate server worker
reconciler`) exists because host processes and a compose `server` contend for port 8000. That
is a `services` concern, and it is destructive against the containerized tier, so a
backends-only bring-up must not run it. Wrap it:

```sh
if [[ "$stage" == "services" ]]; then
  banner "reconcile app tier (never run the kdive:dev containers)"
  docker compose rm -sf migrate server worker reconciler > /dev/null 2>&1 || true
fi
```

**The observability profile** (`up.sh:83-99`) sits between the two blocks deleted above, so
leaving it in place would put prometheus and grafana inside the `backends` stage, which
`just stack-up` never started. Move the whole block after the stage gate below, keeping its
warn-only behavior and its `grafana_supports_arch` check unchanged.

Then gate the rest of the script on the stage, immediately after the migrations phase:

```sh
if [[ "$stage" == "backends" ]]; then
  banner "backends stage complete"
  echo "Backends healthy, bucket verified, schema migrated."
  echo "For libvirt and the host processes: scripts/live-stack/stack-services.sh"
  exit 0
fi
```

Everything after that gate — obs, role bootstrap, libvirt, host processes, inventory
reconcile, status, guidance — is the `services` stage.

The `EUID == 0` refusal (`up.sh:38-41`) and the `.venv` interpreter check (`up.sh:51-54`) run
before the gate and therefore now apply to `just stack-backends`, which `just stack-up` did not
enforce. The venv check is required either way, because the `backends` stage runs
`apply-migrations.sh`. The UID-0 refusal is a deliberate narrowing recorded in ADR-0655's
Consequences; leave it before the gate.

### Step 2.4 — Point the justfile recipe at the script

```just
stack-backends:
    ./scripts/live-stack/stack-services.sh --stage backends
```

The recipe's former body — its own oidc pre-build, `up -d --wait`, `run --rm seaweedfs-init`,
`apply-migrations.sh`, and its closing guidance — is now the script's `backends` stage. Keep
the guidance the recipe used to print by moving it into Step 2.3's stage-complete block.

### Step 2.5 — Update the backend-list guard

`test_up_uses_the_canonical_backend_list` asserted `KDIVE_BACKEND_SERVICES` appeared in the
bring-up script. The script no longer names it — `lib.sh` does. Retarget the guard at the
contract that still matters: that the `--wait` set excludes the one-shot.

```python
def test_wait_set_excludes_the_one_shot() -> None:
    text = (_REPO_ROOT / "scripts" / "live-stack" / "lib.sh").read_text()
    assert "KDIVE_BACKEND_LONG_RUNNING=(postgres seaweedfs oidc)" in text
    assert "KDIVE_BACKEND_SERVICES=(postgres seaweedfs seaweedfs-init oidc)" in text
```

### Verification

- **The one-shot failure fails the stage** — Mode: `focused-test`. Contract: Success criterion
  3. New case in `tests/scripts/test_live_stack_scripts.py` running
  `stack-services.sh --stage backends` with a stub `docker` first on `PATH` that exits 7 on
  `run --rm seaweedfs-init` and 0 otherwise; assert the script exits non-zero. Red against
  Task 1's script, which exits 0. Green:
  `uv run python -m pytest tests/scripts/test_live_stack_scripts.py -q`.
- **`--wait` covers only the long-running three** — Mode: `focused-test`. Same file and stub,
  recording argv; assert the `--wait` invocation names `postgres`, `seaweedfs` and `oidc` and
  not `seaweedfs-init`. Red if the one-shot is folded back into the wait set.
- **Stage selection and flag rejection** — Mode: `focused-test`. Same file: `--stage backends`
  records no role-bootstrap invocation; `--stage nonsense` and
  `--stage backends --skip-libvirt` each exit 2. Red before Step 2.2.
- **The `--wait` set is declared once** — Mode: `focused-test`.
  `test_wait_set_excludes_the_one_shot`. Red while `lib.sh` lacks the new array.
- **`just stack-backends` reaches the script** — Mode: `focused-test`. Same file, asserting the
  recipe body invokes `stack-services.sh --stage backends`. Red before Step 2.4.

### Acceptance criteria

1. `scripts/live-stack/stack-services.sh --stage backends` and the bare invocation both work;
   the former stops after migrations.
2. `just stack-backends` is one line delegating to the script; no compose command remains in
   the justfile recipe.
3. `rg -n 'docker compose' scripts/live-stack/stack-services.sh` shows no backend `up` — only
   the app-tier `rm -sf`, the obs profile, and the role-bootstrap `run --rm`. Because the
   backend `up` now lives in `lib.sh`, Step 1.6's guard must already read both files; confirm
   it does before relying on this criterion.
4. `just ci > /tmp/ci.log 2>&1 < /dev/null` exits 0.

## Rollback

Both tasks are ordinary commits on `refactor/stack-entry-points`; `git revert` restores the
prior names and behavior. No persisted state, schema, or published contract changes, so a
revert needs no data step. Compose volumes are untouched by either task.

## Deferred

- The onboarding-script collapse (`scripts/operations/setup-local-libvirt.sh` versus
  `scripts/live-stack/onboard.sh`), owner: a separate design cycle, not yet filed.
- The worker-lifecycle-contract consolidation (`live_vm_host` templates versus
  `deploy/systemd/install-live-worker-lifecycle.sh`), owner: a separate design cycle, not yet
  filed.
- ~~`.github/workflows/live.yml:523` asserts the bring-up script "owns the whole bring-up
  (backends + bucket ...)", which becomes true only once Task 2 lands. Verify that comment
  reads correctly at the end of Task 2 rather than carrying it forward unread.~~ Verified after
  Task 2: the claim now holds in full, including "+ bucket", which was the part only Task 2's
  `run --rm seaweedfs-init` made true. Comment kept, rewrapped for the longer name.
