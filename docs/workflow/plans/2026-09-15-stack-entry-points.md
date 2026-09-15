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

Expected implementation size: 380–460 changed lines (L) — derived from the file map in Task 1
plus the stage dispatch and readiness function in Task 2.

## Global Constraints

- Python `==3.14.*`; ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict.
- Shell: `set -euo pipefail`, `shfmt -i 2 -d` clean, shellcheck clean (`.shellcheckrc` applies).
- `scripts/live-stack/lib.sh` is **sourced, never executed** (`lib.sh:2-4`): it may define
  variables and functions and must have no other side effects.
- Prose rule: "Milestone", never "Sprint"; avoid "critical", "robust", "comprehensive",
  "elegant" in code comments, docs and commit messages.
- Never edit an append-only record: `docs/adr/`, `docs/debt/`. Never sweep a point-in-time
  record: `docs/archive/`, `docs/superpowers/`, `docs/design/` proof records, merged
  `docs/workflow/` plans.
- Gates: `just lint`, `just type`, `just ci`. Run `just ci` bare, redirected, never piped:
  `just ci > /tmp/ci.log 2>&1 < /dev/null`.
- Branch: `refactor/stack-entry-points`. Base: `main`.

## Task 1 — Rename the entry points and sweep every live reference

Pure rename. No behavior change, so a reviewer can check it by reading names alone and
`git bisect` keeps a working tree at this commit.

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

Expect roughly 66 paths. Split them: every path under `docs/adr/`, `docs/debt/`,
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

Then re-read every touched file and fix the bare references a mechanical substitution misses:
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
_OLD_ENTRY_POINTS = (
    "scripts/live-stack/up.sh",
    "scripts/live-stack/down.sh",
    "scripts/live-stack/status.sh",
    "examples/local-libvirt/up.sh",
    "examples/local-libvirt/down.sh",
    "just stack-up",
)
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
    return [
        p
        for p in out
        if p != "CHANGELOG.md" and not p.startswith(_RECORD_ROOTS)
    ]


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
        for old in _OLD_ENTRY_POINTS:
            if old in text:
                offenders.append(f"{path}: {old}")
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

`tests/live_stack/test_up_invariants.py` reads `up.sh` by path. Point `_UP` at
`scripts/live-stack/stack-services.sh`, rename the module's `_UP` to `_SERVICES` for
readability, and narrow the predicate. The naive form matches two lines that must not fail it:
the retained obs bring-up (`--profile obs up -d prometheus`) and a comment in `lib.sh`.

```python
_SERVICES = _REPO_ROOT / "scripts" / "live-stack" / "stack-services.sh"
_APP_TIER = ("migrate", "server", "worker", "reconciler")
_COMPOSE_UP = re.compile(r"compose\b.*\bup\b")


def _compose_up_lines(text: str) -> list[str]:
    """Non-comment `compose ... up` lines, excluding the observability profile.

    The obs bring-up is out of scope for this guard and legitimately starts prometheus and
    grafana; a comment can mention `compose ... up` without running anything.
    """
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _COMPOSE_UP.search(line):
            continue
        if "--profile obs" in line:
            continue
        lines.append(line)
    return lines


def test_services_never_starts_the_app_tier() -> None:
    for line in _compose_up_lines(_SERVICES.read_text()):
        for svc in _APP_TIER:
            assert not re.search(rf"\b{svc}\b", line, re.IGNORECASE), (
                f"stack-services.sh starts app-tier service in: {line!r}"
            )
```

`re.IGNORECASE` is deliberate: the case-sensitive form passed a `lib.sh` comment containing
`KDIVE_WORKER_COUNT` by accident, and a guard that depends on letter case is not a guard.

Update `test_up_reconciles_app_tier_before_start` and
`test_up_uses_the_canonical_backend_list` to read `_SERVICES`. The latter still holds at this
task, because the rename has not yet changed which services the script names; Task 2 revisits
it.

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
4. No behavior change: `git diff` on the renamed scripts shows only name and comment edits.

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
    if docker image inspect kdive-mock-oidc:dev > /dev/null 2>&1; then
      echo "using cached kdive-mock-oidc:dev — run 'docker rmi kdive-mock-oidc:dev' to force a rebuild after editing deploy/mock-oidc" >&2
    else
      docker compose build oidc
    fi
  fi

  # --wait-timeout is required now the backends carry `restart: on-failure` (ADR-0449): a
  # container that keeps failing cycles Exited -> Restarting instead of settling, so without a
  # bound the convergence poll can block indefinitely rather than reporting.
  # (Comment moved verbatim from the `stack-up` recipe. ADR-0449 is about the systemd
  # `Restart=on-failure` supervision contract, not compose's restart policy; keep the existing
  # citation rather than minting a new claim while relocating code.)
  docker compose up -d --wait --wait-timeout 120 "${KDIVE_BACKEND_LONG_RUNNING[@]}" || return

  # Creates the bucket, enables versioning, verifies Enabled, then exits. `run --rm` so a
  # failure here fails bring-up: the replaced `up -d` form never surfaced this exit status,
  # and a missing bucket then surfaced much later as a worker store check (ADR-0655).
  docker compose run --rm seaweedfs-init
}
```

### Step 2.2 — Add `--stage` to `stack-services.sh`

Parse alongside the existing flags. Default `services`. Reject a stage that is not `backends`
or `services`, and reject `--skip-libvirt` / `--skip-obs` / `--reset-db` under
`--stage backends`, which reaches none of them:

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
  for flag in reset_db skip_obs skip_libvirt; do
    if [[ "${!flag}" == "1" ]]; then
      echo "--stage backends does not reach the phase --${flag//_/-} controls" >&2
      exit 2
    fi
  done
fi
```

Confirm the existing variable names (`reset_db`, `skip_obs`, `skip_libvirt`) against the
script before relying on them; `up.sh` defined `reset_db` and `skip_obs` at minimum.

### Step 2.3 — Replace the inline backend block with the shared function

Delete the oidc pre-build block, the `docker compose up -d "${KDIVE_BACKEND_SERVICES[@]}"`
line, and the 30-iteration postgres health loop. In their place:

```sh
banner "backends"
live_stack_backends_up
```

Then gate the rest of the script on the stage, immediately after the migrations phase:

```sh
if [[ "$stage" == "backends" ]]; then
  banner "backends stage complete"
  echo "Backends healthy, bucket verified, schema migrated."
  echo "For libvirt and the host processes: scripts/live-stack/stack-services.sh"
  exit 0
fi
```

The obs bring-up stays where it is and stays warn-only; it belongs to the `services` stage.

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
   the app-tier `rm -sf`, the obs profile, and the role-bootstrap `run --rm`.
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
- `.github/workflows/live.yml:523` asserts the bring-up script "owns the whole bring-up
  (backends + bucket ...)", which becomes true only once Task 2 lands. Verify that comment
  reads correctly at the end of Task 2 rather than carrying it forward unread.
