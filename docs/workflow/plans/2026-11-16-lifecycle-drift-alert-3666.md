# Lifecycle drift alert Implementation Plan

**Goal:** a scheduled native run failing on lifecycle identity drift files a deduplicated issue.
**Architecture:** `worker-lifecycle.sh compatibility` exits 3 for reprovision-required; the
native spine turns that into a job output; a new report job (modeled on
`.github/workflows/mcp-spec-drift.yml`) files the issue. Spec:
`docs/workflow/specs/2026-11-16-lifecycle-drift-alert-3666-design.md`.
**Tech stack:** bash, GitHub Actions YAML, pytest.

## Global Constraints

- Action pins and `gh` use follow existing workflows; no new actions are added (report job uses
  only `gh`, preinstalled on `ubuntu-latest`).
- Workflow-level `permissions: contents: read` is unchanged. Script-internal names carry no
  `KDIVE_` prefix.
- Expected implementation size: 70–110 changed lines (S) — tests ~50, workflow ~40, script ~6.

File map: `scripts/live-stack/worker-lifecycle.sh` (probe exit codes), `.github/workflows/live.yml`
(spine, job output, new job), `tests/scripts/test_live_stack_scripts.py`,
`tests/scripts/test_live_workflow_shape.py`.

## Task 1: distinct exit status

**Interfaces:** `require_compatible_lifecycle` (existing, `worker-lifecycle.sh:40`) now returns 3
for mismatch/unavailable; the `compatibility` case propagates it; `request` (line 252) keeps
`|| return 1`.

**Verification:** Mode: focused-test — contract: `compatibility` exits 3 on mismatch. Test:
`test_lifecycle_protocol_mismatch_fails_before_mutation` plus new
`test_lifecycle_compatibility_command_exits_3_on_mismatch` in
`tests/scripts/test_live_stack_scripts.py` (copy the success test, install identity
`"0:incompatible"`, assert `returncode == 3`). Red: `assert 1 == 3`. Green:
`just test-verbose tests/scripts/test_live_stack_scripts.py -k lifecycle`.

- [ ] Add the test; confirm red.
- [ ] In `require_compatible_lifecycle` change the two `return 1` after the "installed ... unavailable"
  and "does not match" messages to `return 3`; leave the checkout-probe failure at `return 1`.
- [ ] Confirm green; commit `fix(live): exit 3 from the lifecycle compatibility probe on drift`.

## Task 2: native spine output and report job

**Interfaces:** step id `native-spine` emits `lifecycle_drift`; `jobs.native.outputs.lifecycle_drift`;
new job `lifecycle-drift`.

**Verification:** Mode: focused-test — contract: spine emits the marker, probe stays single and
before `cleanup_required`, propagates status. Test `test_native_spine_reports_lifecycle_drift`
in `tests/scripts/test_live_workflow_shape.py`; red: marker absent. Mode: focused-test —
contract: report job shape. Test `test_lifecycle_drift_job_is_scheduled_only_and_least_privilege`
asserts `needs == "native"`, the `if` text contains `schedule` and `lifecycle_drift == 'true'`,
`permissions == {"contents": "read", "issues": "write"}`, no step uses `actions/checkout`, the
run text contains `--state open` and the exact title, and no other job and not the workflow
grants `issues`. Red: `KeyError`. Green: `just test-verbose tests/scripts/test_live_workflow_shape.py`.
Mode: task-test-not-applicable — live GitHub Issues behaviour; needs the real API, covered by
the structural pins above and `just lint-workflows`.

- [ ] Add tests; confirm red.
- [ ] Replace the bare probe line in the spine heredoc with:
  ```
  lifecycle_rc=0
  scripts/live-stack/worker-lifecycle.sh compatibility || lifecycle_rc=$?
  if [[ "$lifecycle_rc" == 3 ]]; then
    echo "lifecycle_drift=true" >> "$GITHUB_OUTPUT"
  fi
  [[ "$lifecycle_rc" == 0 ]] || exit "$lifecycle_rc"
  ```
  (the existing test counts the string `scripts/live-stack/worker-lifecycle.sh compatibility`
  once; keep it once).
- [ ] Add under `native:` `outputs: lifecycle_drift: ${{ steps.native-spine.outputs.lifecycle_drift }}`.
- [ ] Append job `lifecycle-drift` (ubuntu-latest, `timeout-minutes: 10`,
  `needs: native`, `if: always() && github.event_name == 'schedule' && needs.native.outputs.lifecycle_drift == 'true'`,
  `permissions: {contents: read, issues: write}`, `concurrency: {group: live-native-drift-report, cancel-in-progress: false}`, env `GH_TOKEN`, `GH_REPO`, `RUN_URL`
  built from `github.server_url/repository/actions/runs/run_id`). One step:
  ```
  title="Native live_vm runner lifecycle protocol drift"
  existing="$(gh issue list --state open --search "\"${title}\" in:title" \
    --json number,title --jq "map(select(.title == \"${title}\")) | .[0].number // empty")"
  if [[ -n "$existing" ]]; then echo "already tracked as #${existing}"; exit 0; fi
  gh issue create --title "$title" --label type:bug --label status:needs-triage \
    --body "..."
  ```
  Body: the installed lifecycle protocol does not match this checkout or could not be probed; run link; reprovision the runner
  (operator action) then confirm with a `workflow_dispatch` run; filed by `live.yml`.
- [ ] Confirm green; `just lint-workflows`; commit `feat(ci): file an issue on native lifecycle drift`.

## Self-review

Success 1 → Task 1; 2 and 3 → Task 2. No task without a requirement.
