# Debug transcript directory provisioning (#2955) — plan

Goal: the host MCP server can write `KDIVE_DEBUG_DIR` on a provisioned live host with no manual
step, and the setting declares the server as its reader.
Architecture: one settings scope change, one bring-up step in `stack-services.sh`, one Ansible
role task, one dated ADR-0088 amendment. Spec:
[2026-09-30-debug-dir-provisioning-2955-design.md](../specs/2026-09-30-debug-dir-provisioning-2955-design.md).
Tech stack: Python 3 registry settings, Bash (floor 4.4 on macOS, ADR-0673), Ansible, pytest.

Expected implementation size: 60–110 changed lines (S) — three small source edits, three focused
tests, the regenerated config row, and the ADR amendment.

## Global Constraints

- Directory mode `0750`; owner is the account that starts the host server.
- Shell under `set -euo pipefail`; quote every path.
- `just config-docs` output is committed with the settings change (`config-docs-check`).
- Prose: plain; no "critical", "robust", "comprehensive", "elegant".
- Guardrails: `just lint`, `just type`, `just test-changed`, `just records`
  (after `git fetch origin main`), with `PATH="/opt/homebrew/opt/coreutils/libexec/gnubin:/opt/homebrew/bin:$PATH"`.

## Task 1 — settings scope is the server

Files: `src/kdive/config/core_settings.py`, `docs/guide/reference/config.md` (generated),
`tests/config/test_manifest_completeness.py`.
Interfaces: consumes `DEBUG_DIR`, `_SERVER` (existing, `core_settings.py`). Provides nothing new.

Verification:
- Contract: `DEBUG_DIR.processes` is `{"server"}`. Mode: focused-test.
  Test `test_debug_dir_is_server_scoped` in `tests/config/test_manifest_completeness.py`:
  `assert DEBUG_DIR.processes == frozenset({"server"})`. Red: `{'worker'}` mismatch.
  Green: `uv run pytest tests/config/test_manifest_completeness.py -q -k debug_dir` → 1 passed.
- Contract: generated reference agrees. Mode: focused-test. `just config-docs-check` exits 0.

Steps: write the test; see red; change `processes=_WORKER` to `processes=_SERVER` in
`DEBUG_DIR`; run `just config-docs`; see green; commit both files together.

## Task 2 — stack-services creates the directory

Files: `scripts/live-stack/stack-services.sh`, `tests/scripts/test_live_stack_scripts.py`.
Interfaces: consumes `_run_stack_services(tmp_path, *args, env_extra=...)` (existing test
helper; `sudo` is a stub that logs `REFUSED sudo <args>` to the returned log and exits 0).

Code, inserted before `banner "host processes"`:

```bash
# The host MCP server writes gdb-MI debug transcripts here (#2955, ADR-0088 amendment), and
# /var/lib/kdive is root-owned on a provisioned host. Same skip-or-elevate rule as the provision
# dirs above; outside the libvirt block because --skip-libvirt still starts the server.
debug_dir="${KDIVE_DEBUG_DIR:-/var/lib/kdive/debug}"
if [[ ! -d "$debug_dir" || ! -w "$debug_dir" ]]; then
  sudo install -d -o "$(id -un)" -m 0750 "$debug_dir" || {
    echo "cannot create the debug transcript dir ${debug_dir}: re-run the live_vm_host play," >&2
    echo "or create it owned by $(id -un) with mode 0750" >&2
    exit 1
  }
fi
```

The existing test `test_services_stage_reconciles_the_app_tier` asserts no `REFUSED` privileged
call under `--stage services --skip-libvirt`; add
`env_extra={"KDIVE_DEBUG_DIR": str(tmp_path)}` (writable) to its `_run_stack_services` call.

Verification:
- Contract: an absent directory is created through `sudo install -d` with owner and mode.
  Mode: focused-test. Test `test_services_bring_up_creates_the_debug_dir`: run
  `_run_stack_services(tmp_path, "--skip-libvirt", env_extra={"KDIVE_SKIP_OBS": "1",
  "KDIVE_DEBUG_DIR": str(tmp_path / "debug"), "KDIVE_WORKER_COUNT": "bad"})`.
  `KDIVE_WORKER_COUNT=bad` stops `restart_host_processes` at its first line, before any process
  is touched. Assert the log holds `REFUSED sudo install -d -o <user> -m 0750
  <tmp_path>/debug`, where `<user>` is `subprocess.run(["id", "-un"], ...)` output (the script's
  own source). Red: no such line.
- Contract: a writable directory needs no sudo. Mode: focused-test. Test
  `test_services_bring_up_skips_a_writable_debug_dir`: same call with the directory made first;
  assert no `REFUSED sudo install` line names it.
Green: `uv run pytest tests/scripts/test_live_stack_scripts.py -q -k
"debug_dir or reconciles_the_app_tier"` → 3 passed.

## Task 3 — live_vm_host role creates the directory

Files: `deploy/ansible/roles/live_vm_host/tasks/main.yml`,
`tests/deploy/test_live_worker_provisioning.py`.
Interfaces: consumes `MAIN_TASKS`, `_text()` (existing test helpers), and the role variable
`live_vm_host_operator_user`.

Code, after the task `Import reusable shared provider directories`:

```yaml
- name: Create the server debug transcript directory
  ansible.builtin.file:
    path: /var/lib/kdive/debug
    state: directory
    owner: "{{ live_vm_host_operator_user }}"
    group: "{{ live_vm_host_operator_user }}"
    mode: "0750"
    follow: false
```

Verification:
- Contract: the task shape. Mode: focused-test. Test
  `test_role_creates_the_server_debug_directory` loads `yaml.safe_load(_text(MAIN_TASKS))`,
  finds the task by name, and asserts the five `ansible.builtin.file` keys above. Red:
  `StopIteration`. Green: `uv run pytest tests/deploy/test_live_worker_provisioning.py -q -k
  debug_directory` → 1 passed. `just lint` includes ansible-lint.

## Task 4 — ADR-0088 amendment

File: `docs/adr/0088-deployment-packaging.md`. Append `### Amendment (2026-09-30): the server
writes debug transcripts (#2955)`: the server, not the worker, reads `KDIVE_DEBUG_DIR`; live
hosts get it from `stack-services.sh` and the `live_vm_host` role; Helm/compose server volumes
stay with the operator follow-up.
Verification: Mode: task-test-not-applicable — ADR prose has no executable consumer; `just
records` checks the record shape.

## Task 5 — live proof (no commit)

On the lease-held ppc64le host: stop the stack; `sudo mv` any existing `/var/lib/kdive/debug`
into `~/kdive-2955-proof` (old transcripts are kept there, not restored); show it absent; run
`stack-services.sh` from a detached worktree at the branch HEAD; `stat -c '%U %a'` the directory;
run `uv run pytest "tests/integration/test_live_stack.py::test_spine_over_the_wire" -m live_stack`
(its `attach` phase calls `debug.read_registers`); record results; restart the stack in normal
mode from `/opt/kdive`. The new directory stays: it is the provisioned state.
