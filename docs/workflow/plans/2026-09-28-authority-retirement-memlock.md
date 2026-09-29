# Authority retirement memlock plan (#2891)

Goal: retirement completes without the authority account and restarts a user manager that still
holds the retired memlock ceiling. Architecture: two Ansible task lists change; structural pytest
proofs and a Jinja rendering proof pin them; [ADR-0708](../../adr/0708-authority-retirement-restarts-the-user-manager.md)
and the [spec](../specs/2026-09-28-authority-retirement-memlock-design.md) hold the decision.
Tech: Ansible (ansible-core 2.21.1 per `just test-ansible`), pytest, PyYAML, Jinja2.

Expected implementation size: 90–130 changed lines (S) — two task files (~50), two test files
(~60), runbook paragraph (~5).

## Global Constraints

- Branch `feat/authority-retirement-memlock-2891`, base `main`; edit only the spec's surface.
- Guardrails: `just lint`, `just type`, `just test-changed`, `just lint-ansible`, `just records`.
- Known-account guard text, exactly: `ansible_facts.getent_passwd[authority_account] | default(none) is not none`.

## Task 1 — Teardown guard form and ceiling restart

Files: `deploy/ansible/playbooks/authority_host_teardown.yml`,
`tests/deploy/test_live_worker_provisioning.py`.

Verification:
- `Mode: focused-test` — guard contract: new
  `test_authority_teardown_passwd_guards_tolerate_a_missing_account` collects every task whose
  YAML dump contains `getent_passwd[authority_account][1]` and asserts its first `when` item is the
  known-account text. It evaluates that text with
  `jinja2.Environment(undefined=StrictUndefined).compile_expression` (native booleans) and
  `authority_account="kdive-provider-authority"`: `False` for
  `ansible_facts={"getent_passwd": {"kdive-provider-authority": None}}`, `True` for
  `["x", "981"]`. It evaluates the two passwd clauses of `Assert authority services and
  processes are inactive` (the clauses after the first) with the `None` entry and no other
  variables: `True`, because the known-account half short-circuits. No task contains
  ` in ansible_facts.getent_passwd` (`yaml.safe_dump(..., width=10**6)`). Red today: the
  five first items are `authority_account in ansible_facts.getent_passwd`. Green:
  `uv run python -m pytest tests/deploy/test_live_worker_provisioning.py -q -k teardown`.
- `Mode: focused-test` — restart contract: new
  `test_authority_teardown_restarts_a_user_manager_holding_the_retired_ceiling` asserts the four
  tasks below exist with those argv/modules and `when` lists, and that the order is
  `Assert authority services and processes are inactive` < drop-in removal <
  `Reload systemd after authority unit removal` < PID read < restart. Red: task not found.

Steps:
1. Write both tests; run the focused command; expect 2 failures.
2. Replace the guards at the five `when` sites with the known-account text; rewrite the two
   assert clauses as `ansible_facts.getent_passwd[authority_account] | default(none) is none or
   <existing right-hand side>`.
3. Append after `Reload the authority user manager after unit removal`:

```yaml
    - name: Read the authority user manager main PID
      ansible.builtin.command:
        argv: [/usr/bin/systemctl, show, --property=MainPID, --value,
               "user@{{ ansible_facts.getent_passwd[authority_account][1] }}.service"]
      register: authority_user_manager_pid
      changed_when: false
      when: ansible_facts.getent_passwd[authority_account] | default(none) is not none
    - name: Read the authority user manager configured memlock limit
      ansible.builtin.command:
        argv: [/usr/bin/systemctl, show, --property=LimitMEMLOCK, --value,
               "user@{{ ansible_facts.getent_passwd[authority_account][1] }}.service"]
      register: authority_user_manager_configured_memlock
      changed_when: false
      when: ansible_facts.getent_passwd[authority_account] | default(none) is not none
    - name: Read the authority user manager running memlock limit
      ansible.builtin.command:
        argv: [/usr/bin/prlimit, --memlock, --noheadings, --output=HARD,
               "--pid={{ authority_user_manager_pid.stdout }}"]
      register: authority_user_manager_running_memlock
      changed_when: false
      when:
        - ansible_facts.getent_passwd[authority_account] | default(none) is not none
        - authority_user_manager_pid.stdout not in ["", "0"]
    - name: Restart the authority user manager to retire its memlock ceiling
      ansible.builtin.systemd_service:
        name: "user@{{ ansible_facts.getent_passwd[authority_account][1] }}.service"
        state: restarted
      when:
        - ansible_facts.getent_passwd[authority_account] | default(none) is not none
        - authority_user_manager_pid.stdout not in ["", "0"]
        - authority_user_manager_running_memlock.stdout | trim == 'unlimited'
        - authority_user_manager_configured_memlock.stdout | trim != 'infinity'
```

4. Run the focused command (2 pass), `just lint-ansible`; commit
   `fix(deploy): tolerate a missing authority account and retire its memlock ceiling`.

## Task 2 — Role disable path

Files: `deploy/ansible/roles/provider_authority_host/tasks/disable.yml`,
`tests/deploy/test_provider_authority_local_provisioning.py`.

Verification:
- `Mode: focused-test` — extend `test_retiring_the_authority_removes_its_memlock_drop_in`: the
  same four task names exist with the same conditions, keyed on
  `ansible_facts.getent_passwd['kdive-provider-authority'] | default(none) is not none`; order is
  removal < `Reload systemd before reading the retiring authority user manager` (module
  `ansible.builtin.systemd_service: {daemon_reload: true}`, no `when`) < PID read < restart <
  `Retire authority user-service boot persistence` < `Apply authority removal before completing
  cleanup`. Red: task not found. Green:
  `uv run python -m pytest tests/deploy/test_provider_authority_local_provisioning.py -q -k memlock`.

Steps:
1. Extend the test; expect red.
2. After the drop-in removal insert the reload task, then the four tasks from Task 1 named
   `... retiring authority user manager ...`, with `authority_account` replaced by
   `'kdive-provider-authority'`, the command paths unchanged, and registers prefixed
   `provider_authority_host_retired_`; the linger task and flush follow unchanged.
3. Green, `just lint-ansible`; commit `fix(deploy): retire the authority memlock ceiling on disable`.

## Task 3 — Runbook

`docs/operating/runbooks/self-hosted-kvm-runner.md`, retirement paragraph only: add that teardown
tolerates an absent authority account and restarts a user manager still holding the retired
memlock ceiling, which ends any process it still runs.
`Mode: task-test-not-applicable` — prose with no executable consumer. Commit `docs: ...`.
