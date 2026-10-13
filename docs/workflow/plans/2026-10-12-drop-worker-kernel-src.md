# Drop the worker kernel-source requirement: implementation plan (#3193)

**Goal:** fixed live workers start on a host with no kernel checkout.

**Architecture:** delete the unread `source_root` setting at every layer it crosses: the
launcher, the lifecycle request contract, the witness's `worker.env`, the gate's pass-through,
the config registry, and host preparation. Keep `KDIVE_KERNEL_SRC` as the live-proof and script
kernel-fixture variable. The spec is `docs/workflow/specs/2026-10-12-drop-worker-kernel-src-design.md`.

**Tech stack:** Python 3.14 (pydantic), bash, Ansible, pytest.

Expected implementation size: 150–230 changed lines (M) — summed from the file map below
(about 70 source/script/playbook lines removed or edited, about 60 test lines, and the
regenerated config reference).

## Global Constraints

- `LIFECYCLE_PROTOCOL_VERSION` stays `1`. Only the schema hash moves (spec, Rollout).
- No new dependency, setting, or environment variable.
- Do not touch `scripts/live-vm/preflight-env.sh`, `tests/integration/test_live_stack.py`, or
  the `.github/workflows/live.yml` kernel fetches. These are approved exclusions.
- The tool shell is zsh, so run multi-line shell verification under `bash`. Run gates bare.
  `just ci > file 2>&1 < /dev/null` is the full gate.

## File map

| File | Change |
|------|--------|
| `scripts/live-stack/worker-lifecycle.sh` | drop the kernel checks, the `KDIVE_SOURCE_ROOT` handoff, and the `source_root` key |
| `src/kdive/processes/lifecycle/systemd/systemd_worker_contract.py` | drop `source_root` and its two validator registrations |
| `src/kdive/processes/lifecycle/systemd/systemd_worker_state.py` | drop two `worker.env` entries |
| `deploy/systemd/bin/kdive-live-worker-gate` | drop two allowlisted names |
| `src/kdive/config/core_settings.py` | delete `KERNEL_SRC` and its registry entry |
| `src/kdive/config/external_env.py` | add `KDIVE_KERNEL_SRC` (test), delete `KDIVE_SOURCE_ROOT` and `KDIVE_WORKER_SOURCE_ROOT` |
| `docs/guide/reference/config.md` | regenerate with `just config-docs` |
| `scripts/live-stack/env.sh` | rewrite the stale comment only |
| `deploy/ansible/playbooks/local-libvirt-host.yml` | drop the kernel var, its fact, and the kernel traversal item |
| `deploy/ansible/tests/run-local-libvirt-host.py` | drop the kernel assertions, keep the checkout traversal proof |
| `examples/local-libvirt/{demo-up.sh,env.sh,README.md}` | summary line, message, comments, prerequisite |
| tests | `tests/processes/lifecycle/systemd/{systemd_worker_support.py,test_systemd_worker_contract.py,test_systemd_worker_state.py,test_systemd_worker_lifecycle.py}`, `tests/deploy/test_live_worker_gate.py`, `tests/scripts/test_live_stack_scripts.py`, `tests/live_vm/test_systemd_worker_lifecycle.py` |

## Task 1: remove `source_root` from the lifecycle protocol

All four layers change in one commit. A launcher that omits the field against a contract that
requires it, or the reverse, is one broken protocol, not two reviewable steps.

**Interfaces:** `WorkerSettings` loses `source_root: str`. No other signature changes.
`_lifecycle_start_with_inventory(tmp_path, inventory, *, access_ok=True, extra_env=None)` in
`tests/scripts/test_live_stack_scripts.py` keeps its signature.

**Verification:**

- Contract rejects `source_root`. Mode: focused-test. Add
  `test_start_rejects_the_removed_source_root_setting` to `test_systemd_worker_contract.py`.
  It sets `settings["source_root"] = "/srv/kdive"` on `start_payload()` and expects
  `ValidationError` matching `extra_forbidden`. It is red before step 3 because the field is
  accepted. Green: `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_contract.py`.
- `worker.env` omits both names. Mode: focused-test. Delete the `KDIVE_KERNEL_SRC=/srv/kdive`
  and `KDIVE_WORKER_SOURCE_ROOT=/srv/kdive` lines from `_ENVIRONMENT_WITHOUT_INVENTORY` in
  `test_systemd_worker_state.py`. `test_environment_carries_the_inventory_only_when_one_is_sent`
  is red until the witness stops rendering them. Green: `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_state.py`.
- The gate drops the names. Mode: focused-test. In `tests/deploy/test_live_worker_gate.py`, keep
  both names in the input fixture (lines 65 and 90), which models a pre-upgrade `worker.env`.
  Delete them from the expected worker environment (lines 274 and 312). Red: the gate still
  passes them through. Green: `just test-verbose tests/deploy/test_live_worker_gate.py`.
- The launcher builds a start without a kernel tree. Mode: focused-test. In
  `_lifecycle_start_with_inventory`, delete the `"KDIVE_KERNEL_SRC": "/tmp/kernel",` entry, then
  add `env.pop("KDIVE_KERNEL_SRC", None)` after the `KDIVE_SECRETS_ROOT` pop so an inherited
  developer value cannot mask the proof. Red: `test_lifecycle_start_omits_the_inventory_for_a_local_only_stack`
  fails with `lifecycle request construction failed safely`, because `source_root=""` is not
  absolute. Green: `just test-verbose tests/scripts/test_live_stack_scripts.py -k lifecycle_start`.

**Steps:**

1. Make the four test edits above, plus these mechanical ones:
   - Delete `"source_root": "/srv/kdive",` from `systemd_worker_support.py`.
   - Delete `source_root="/src/kdive",` from `_settings()` in `test_systemd_worker_lifecycle.py`.
   - In `test_systemd_worker_contract.py`, switch the UTF-8 boundary cases from `source_root` to
     `rootfs_dir`, keeping their values, assertions, and `test_start_rejects_relative_worker_paths`
     intact.
   - Delete the remaining `"KDIVE_KERNEL_SRC": "/tmp/kernel",` entries at the two other launcher
     harnesses in `test_live_stack_scripts.py`. They stub `require_start_prerequisites`.
2. Run the four green commands above and confirm each named test is red for the stated reason.
3. Edit the sources:

   ```diff
   # systemd_worker_contract.py (WorkerSettings fields and both field_validator lists)
   -    source_root: str
   -        "source_root",
   # systemd_worker_state.py (_environment)
   -            "KDIVE_KERNEL_SRC": settings.source_root,
   -            "KDIVE_WORKER_SOURCE_ROOT": settings.source_root,
   # kdive-live-worker-gate (_WORKER_ENV_NAMES)
   -        "KDIVE_KERNEL_SRC",
   -        "KDIVE_WORKER_SOURCE_ROOT",
   # worker-lifecycle.sh (require_start_prerequisites, request)
   -  [[ "${KDIVE_KERNEL_SRC:-}" == /* && -d "${KDIVE_KERNEL_SRC}" ]] || {
   -    echo "worker kernel source must be an existing absolute directory" >&2
   -    return 1
   -  }
   -  require_worker_path_access "${KDIVE_KERNEL_SRC}" rx "worker kernel source" || return 1
   -    KDIVE_SOURCE_ROOT="${KDIVE_KERNEL_SRC:-}" \
   -                    "source_root": os.environ["KDIVE_SOURCE_ROOT"],
   ```

4. Remove the kernel prerequisite from the native proof in
   `tests/live_vm/test_systemd_worker_lifecycle.py` `_assert_prerequisites`: delete the three
   `kernel_source` lines. This is `task-test-not-applicable`: it is a live-only prerequisite
   assertion, it cannot run without the installed host, and no ordinary test can observe it.
5. Rerun the four green commands, plus
   `just test-verbose tests/processes/lifecycle/systemd tests/deploy/test_live_worker_provisioning.py`,
   `just lint`, `just type`, and `just lint-shell`. All should pass. Then commit
   `fix(lifecycle): drop the unread worker kernel source root`.

**Acceptance:** `rg -n 'source_root|KDIVE_SOURCE_ROOT|WORKER_SOURCE_ROOT' src/kdive/processes scripts/live-stack deploy/systemd/bin`
matches nothing. All lifecycle tests are green.

## Task 2: retire the `KERNEL_SRC` setting

**Verification:**

- Registry and catalogue. Mode: focused-test. `just config-docs-check` must fail until
  `config.md` is regenerated, and `just env-docs-check` must pass with the variable catalogued
  only in `external_env.py`. Red: run `just env-docs-check` after deleting the setting but
  before adding the catalogue entry. It names `KDIVE_KERNEL_SRC` as undocumented, because
  scripts still read it.
- `scripts/live-stack/env.sh` comment. Mode: task-test-not-applicable. It is a comment-only edit,
  and the default logic below it is unchanged.

**Steps:**

1. Delete the `KERNEL_SRC = Setting(...)` block (lines 491–498) and its `KERNEL_SRC,` entry
   (line 1079) from `src/kdive/config/core_settings.py`. Confirm `rg -n '\bKERNEL_SRC\b' src`
   now matches nothing.
2. Run `just env-docs-check`, and expect the red described above.
3. In `src/kdive/config/external_env.py`, add this entry after `KDIVE_GUEST_IMAGE`:

   ```diff
   +    ExternalEnvVar(
   +        "KDIVE_KERNEL_SRC",
   +        "test",
   +        "~/src/linux when that directory exists",
   +        "Built kernel tree the live proofs, `scripts/live-vm/preflight-env.sh`, and "
   +        "`scripts/operations/live_debug_build.py` read; `scripts/live-stack/env.sh` supplies the "
   +        "default. Workers never read it (ADR-0316).",
   +    ),
   ```

   Delete the `KDIVE_SOURCE_ROOT` and `KDIVE_WORKER_SOURCE_ROOT` entries.
4. Replace the `scripts/live-stack/env.sh` comment above the `KDIVE_KERNEL_SRC` block with:

   ```text
   # KDIVE_KERNEL_SRC: the built kernel tree the live proofs and operator scripts read as their
   # fixture (workers never read it; ADR-0316). An explicit value is honored verbatim. The
   # HOME-relative default ${HOME}/src/linux is exported only when it exists, so a privileged
   # shell ($HOME -> /root) leaves it unset and each proof's own preflight names the gap.
   ```

5. Run `just config-docs`, then `just config-docs-check`, `just env-docs-check`, `just lint`,
   `just type`, and `just lint-shell`. All should pass. Then
   `just test-verbose tests/config tests/scripts/test_live_stack_scripts.py`. Commit
   `refactor(config): catalogue KDIVE_KERNEL_SRC as a live-proof variable`.

## Task 3: stop granting traversal to a kernel tree during host preparation

**Verification:** Mode: focused-test. Update `deploy/ansible/tests/run-local-libvirt-host.py`:

- Delete the `getent_passwd` and `Resolve the kernel source` assertion (lines 204–208).
- Add `require("Resolve the kernel source from the validated operator account" not in pre_tasks, "host preparation must not resolve a kernel tree")`.
- Add `require("local_libvirt_host_kernel_source" not in play["vars"], "host preparation must not take a kernel tree")`.
- Rename the lookups to the new task names, `Inspect the checkout root` and
  `Make every checkout ancestor traversable by workers`.
- Reduce the traversal fixture to one play with `vars: {"local_libvirt_host_source": str(source)}`.
  Keep the two-run idempotence loop. Assert `fixture` and `source` end at `0o701` and `/tmp` is
  unchanged. Delete the kernel, link, dangling, and regular cases, which exercised only the
  optional kernel root.

Red: the new `not in` requirements fail against the unedited playbook. Green:
`uv run --with 'ansible-core==2.21.1' python3 deploy/ansible/tests/run-local-libvirt-host.py`,
which prints both `local-libvirt-host:` pass lines.

**Steps:**

1. Make the test edits, run the green command, and confirm the red.
2. In `deploy/ansible/playbooks/local-libvirt-host.yml`:
   - Delete `local_libvirt_host_kernel_source: ""`.
   - Delete the `Resolve the kernel source from the validated operator account` pre-task.
   - Rename `Inspect the checkout and kernel roots` to `Inspect the checkout root`, and delete
     its kernel loop item.
   - Rename the traversal task to `Make every checkout ancestor traversable by workers`.
3. Run the green command, then `just lint-ansible` and `just test-ansible`. All should pass. Commit
   `fix(ansible): stop opening kernel-tree ancestors to worker accounts`.

## Task 4: the demo no longer implies a kernel tree

**Verification:**

- `demo-up.sh` has no unguarded `${KDIVE_KERNEL_SRC}` under `set -u`. Mode: focused-test. Extend
  `test_demo_up_installs_mcp_json_into_demo_workspace_not_kernel_src` with
  `assert "${KDIVE_KERNEL_SRC}" not in text`. Red against the current `Kernel    :` line. Green:
  `just test-verbose tests/scripts/test_live_stack_scripts.py -k demo`.
- The README, `env.sh` comments, and the `demo-up.sh` hint text. Mode: task-test-not-applicable.
  They are operator prose with no executable consumer, and the workspace fallback logic is
  unchanged and pinned by the existing `test_local_libvirt_example_demo_workspace_*` tests.

**Steps:**

1. Add the assertion and confirm the red.
2. In `examples/local-libvirt/demo-up.sh`:
   - Delete the `  Kernel    : ${KDIVE_KERNEL_SRC}` summary line.
   - Replace the unset-workspace message with:

     ```text
     KDIVE_DEMO_WORKSPACE is unset (it defaults to KDIVE_KERNEL_SRC, which is also unset);
     export KDIVE_DEMO_WORKSPACE to the directory you open your MCP client in -- any directory
     works, a kernel tree is not required -- then re-run to install the .mcp.json (the stack is up).
     ```

   - In the step-5 comment, change "defaults to the kernel tree" to "defaults to KDIVE_KERNEL_SRC
     when set".
3. In `examples/local-libvirt/env.sh`, change the header to "(DB / OIDC / S3 / HTTP / kernel
   fixture)". In the second comment, replace "It already exports KDIVE_KERNEL_SRC=~/src/linux" with
   "It exports KDIVE_KERNEL_SRC=~/src/linux when that tree exists".
4. In `examples/local-libvirt/README.md`:
   - Delete the `A kernel source tree at KDIVE_KERNEL_SRC` prerequisite bullet.
   - Rewrite the workspace bullet as: "A directory to open your MCP client in, at
     `KDIVE_DEMO_WORKSPACE` (default `KDIVE_KERNEL_SRC` when set; required otherwise). The stack
     itself needs no kernel tree."
   - Change the `KDIVE_KERNEL_SRC` table row's meaning to "Your kernel tree, if any: the default
     workspace and the live proofs' kernel fixture. The workers never read it."
   - Leave the intro and walkthrough's `~/src/linux` example as it is, since it shows a typical
     workspace.
5. Run the green command, `just lint-shell`, and `just docs-links`. All should pass. Commit
   `docs(local-libvirt): the demo needs a workspace, not a kernel tree`.

## Task 5: full gate and live proof

1. Run `just ci > <scratch>/ci.log 2>&1 < /dev/null`. Expect exit 0.
2. Live proof on the assigned lab x86_64 guest, following the dispatcher's host contract:
   - Deploy the base commit, run `just prepare-local-libvirt-host`, and start workers with a
     placeholder `KDIVE_KERNEL_SRC`, which gives the base-provisioned lifecycle venv.
   - Check out the candidate and run `worker-lifecycle.sh status`. Expect
     `installed lifecycle protocol does not match this checkout; reprovision the runner`.
   - Stop the stack and remove the placeholder. Reprovision with the candidate's
     `just prepare-local-libvirt-host`, then open a fresh login.
   - With no `~/src/linux` and `KDIVE_KERNEL_SRC` unset, run `KDIVE_DEMO_WORKSPACE=<empty dir>
     examples/local-libvirt/demo-up.sh`. Expect exit 0 and all slots started.
   - Run one catalog-image provision, then tear down.
   - If the base-venv arm cannot run on the guest, record why and run only the candidate arm.

## Spec coverage

| Spec item | Task |
|-----------|------|
| Launcher, contract, witness, gate (1–4) and Rollout | 1, 5 |
| Configuration (5), comment (8) | 2 |
| Host preparation (6) | 3 |
| Demo (7) | 4 |
| Live Success line | 5 |
