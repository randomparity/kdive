# Authority lane TLS expiry check and bounded restart (#3272) — plan

Goal: provisioning refuses authority/worker TLS certificates that are expired or inside a
margin, and the authority unit stops restarting after a bounded number of failed starts.
Architecture: one shared `live_vm_host` task file runs `openssl x509 -checkend` per
certificate and is included by preflight (controller sources) and verify (installed files);
three renderings of the authority unit gain a `[Unit]` start limit.
Tech: Ansible (ansible-core 2.21.1), systemd, Python 3.14 tests (pytest, `cryptography`).
Spec: `docs/workflow/specs/2026-10-19-authority-tls-expiry-design.md`.

Expected implementation size: 240–300 changed lines (M) — two tasks: ~50 YAML task lines,
~15 unit lines, ~110 harness lines, ~60 unit-test lines, ~10 doc/recipe lines and the
regenerated fixture.

## Global constraints

- Role default `live_vm_host_authority_tls_expiry_margin_seconds: 43200`; it must stay
  below the carrier's 86400 s certificate lifetime.
- `StartLimitIntervalSec=600`, `StartLimitBurst=5` in `[Unit]`; `RestartSec=5s` and
  `TimeoutStartSec=90s` unchanged.
- Never pass a key path to the expiry check; no `no_log` on expiry tasks.
- Guardrails: `just lint`, `just type`, `just lint-ansible`, `just test-ansible`, focused
  pytest; final managed pre-push `just ci > log 2>&1 < /dev/null`. Commands run in bash
  with `set -o pipefail` from the worktree root.

## File map

| File | Change | Criterion |
|---|---|---|
| `deploy/ansible/roles/live_vm_host/tasks/tls_expiry.yml` | new: margin assert, `openssl` read, per-cert assert | 1–4 |
| `deploy/ansible/roles/live_vm_host/defaults/main.yml` | add margin default | 4 |
| `deploy/ansible/roles/live_vm_host/tasks/authority_preflight.yml` | two includes, delegated to localhost | 1, 3 |
| `deploy/ansible/roles/live_vm_host/tasks/verify.yml` | two includes on installed paths | 2, 3 |
| `deploy/ansible/tests/run-authority-tls-expiry.py` | new harness over the real task file | 1–3 |
| `justfile`, `deploy/ansible/tests/README.md` | wire and list the harness | 1–3 |
| `deploy/ansible/tests/fixtures/runner-tasks-2391.txt`, `run-local-worker-host.py` | regenerate listing and count | existing contract |
| three authority unit renderings | `[Unit]` start limit | 5 |
| `tests/deploy/test_provider_authority_local_provisioning.py` | wiring and unit tests | 1, 2, 5 |
| `docs/operating/runbooks/live-testing.md` | check, knob and `reset-failed` sentence | 3 |

## Task 1 — expiry check

Interfaces: consumes role vars `live_vm_host_authority_*_source`,
`live_vm_host_worker_authority_*_source`, `live_vm_host_authority_credentials_dir`,
`live_vm_host_worker_authority_secrets_root`. Provides `tls_expiry.yml` with inputs
`live_vm_host_tls_expiry_certificates: list[{name: str, path: str}]` and
`live_vm_host_authority_tls_expiry_margin_seconds`.

Verification:

- Contract: certificate verdicts and messages. Mode: focused-test.
  `deploy/ansible/tests/run-authority-tls-expiry.py`; red: fails because
  `tls_expiry.yml` does not exist; green:
  `uv run --with 'ansible-core==2.21.1' python3 deploy/ansible/tests/run-authority-tls-expiry.py`
  prints `ok authority-tls-expiry: 7 cases`.
- Contract: call-site wiring (exact certificate lists, preflight delegation, no key paths,
  margin below carrier lifetime). Mode: focused-test.
  `tests/deploy/test_provider_authority_local_provisioning.py::test_tls_expiry_checks_cover_authority_and_worker_certificates`;
  red: no include of `tls_expiry.yml` found; green: `uv run python -m pytest <that id> -q`.
- Contract: runner task listing. Mode: focused-test. `run-local-worker-host.py`; red:
  count/fixture mismatch after the verify includes; green after regeneration via
  `uv run --with 'ansible-core==2.21.1' python3 deploy/ansible/tests/run-local-worker-host.py`.

Steps:

1. Write the harness. For each case it writes certificates generated with `cryptography`
   (EC P-256, self-signed, `not_valid_after` = now + offset) into a temporary directory,
   writes a one-play playbook (`hosts: localhost`, `connection: local`,
   `gather_facts: false`, `ansible_python_interpreter: sys.executable`, play vars for the
   margin and certificate list, tasks: `import_tasks: <role>/tasks/tls_expiry.yml` then a
   `debug` marker `tls-expiry-completed`), runs `ansible-playbook -i localhost,` with
   `ANSIBLE_CONFIG=deploy/ansible/ansible.cfg` and stdin `/dev/null`, and asserts exit
   status, marker presence and expected text. Cases (margin 43200 unless stated):
   `valid` (+2 d) passes; `inside-margin` (+6 h) fails with `TLS credential server-certificate`
   and `notAfter=`; `expired` (−1 d) fails with `notAfter=`; `mixed` (server-ca +2 d,
   health-client-certificate −1 d) fails naming only `health-client-certificate`;
   `not-a-certificate` fails with `is not a readable PEM certificate`; `bad-margin`
   (`"-1"`) fails with `must be a non-negative whole number`; `openssl-absent` (play
   `environment: {PATH: <empty temporary directory>}`) fails with `could not be checked with
   openssl`. Run it: red.
2. Create `tls_expiry.yml`:

   ```yaml
   ---
   - name: Require a whole-second TLS expiry margin
     ansible.builtin.assert:
       that:
         - live_vm_host_authority_tls_expiry_margin_seconds | string is match('^[0-9]+$')
       fail_msg: >-
         live_vm_host_authority_tls_expiry_margin_seconds must be a non-negative whole number
         of seconds, not {{ live_vm_host_authority_tls_expiry_margin_seconds }}.
       quiet: true

   - name: Read each TLS certificate's notAfter against the expiry margin
     ansible.builtin.command:
       argv: [openssl, x509, -noout, -enddate, -checkend,
              "{{ live_vm_host_authority_tls_expiry_margin_seconds | string }}",
              -in, "{{ item.path }}"]
     loop: "{{ live_vm_host_tls_expiry_certificates }}"
     loop_control:
       label: "{{ item.name }}"
     register: live_vm_host_tls_expiry_results
     changed_when: false
     failed_when: false
     check_mode: false

   - name: Require each TLS certificate to outlive the expiry margin
     ansible.builtin.assert:
       that:
         - item.rc == 0
         - "'Certificate will not expire' in item.stdout_lines"
       fail_msg: >-
         {% set end = item.stdout_lines | select('match', '^notAfter=') | first | default('') -%}
         TLS credential {{ item.item.name }} ({{ item.item.path }})
         {% if end %}is expired or expires within the
         {{ live_vm_host_authority_tls_expiry_margin_seconds }}-second margin ({{ end }}).
         Renew it before use; see docs/operating/runbooks/live-testing.md, Candidate evidence
         and cleanup.{% elif item.rc == 1 %}is not a readable PEM certificate:
         {{ item.stderr | trim }}.{% else %}could not be checked with openssl:
         {{ (item.stderr or item.msg | default('')) | trim }}.{% endif %}
       quiet: true
     loop: "{{ live_vm_host_tls_expiry_results.results }}"
     loop_control:
       label: "{{ item.item.name }}"
   ```

3. Add `live_vm_host_authority_tls_expiry_margin_seconds: 43200` to defaults after
   `live_vm_host_authority_credentials_dir`, with a comment that it stays below the
   carrier's one-day certificates.
4. In `authority_preflight.yml`, after `Require protected regular fixed-worker authority
   client sources`, add `Require fixed-worker authority client certificates to outlive the
   expiry margin` (`include_tasks` `file: tls_expiry.yml`, `apply: {delegate_to: localhost,
   become: false}`, `when: live_vm_host_worker_authority_enabled | bool`, certificates
   `server-ca` and `client-certificate` from the two worker certificate sources). Inside the
   authority block, after `Require protected regular external-boot authority source files`,
   add `Require external-boot authority certificates to outlive the expiry margin` with the
   same `apply` and certificates `server-certificate`, `server-ca`, `worker-client-ca`,
   `health-client-certificate` from their `_source` vars.
5. In `verify.yml`, immediately before `Assert the authority service is ready`, add
   `Require installed authority certificates to outlive the expiry margin` (the four names
   under `live_vm_host_authority_credentials_dir`, `when: live_vm_host_authority_enabled |
   bool`) and `Require installed fixed-worker authority certificates to outlive the expiry
   margin` (`server-ca`, `client-certificate` under
   `live_vm_host_worker_authority_secrets_root`, `when: live_vm_host_worker_authority_enabled
   | bool`). No delegation. Placing them first keeps the named message ahead of the generic
   readiness failure an expired certificate causes.
6. Write the wiring test: load both YAML files, collect tasks whose
   `ansible.builtin.include_tasks.file` is `tls_expiry.yml` (descending into `block`),
   compare each `vars.live_vm_host_tls_expiry_certificates` name list exactly, require both
   preflight includes to carry `apply == {delegate_to: localhost, become: false}` and verify
   includes none, require each verify include to precede `Assert the authority service is
   ready`, require no path expression to contain `key`, and require the default
   margin to be an int below 86400 while `prove-external-boot-authority-host.sh` still signs
   with `-days 1`.
7. Regenerate the runner fixture and count per the harness's own filter; wire the harness
   into `test-ansible` after `run-remote-egress-preflight.py` and add its README row; add
   the runbook sentence (check, knob, default, controller `openssl` prerequisite,
   `systemctl reset-failed` within the start limit window) to the paragraph ending "including credential expiry and database grants".
8. Run all three focused commands green, `just lint`, `just type`, `just lint-ansible`;
   commit `feat(deploy): refuse authority TLS certificates inside the expiry margin`.

## Task 2 — bounded authority restart

Interfaces: none consumed; changes unit text and the drift proof's `always` block in
`deploy/ansible/roles/live_vm_host/tasks/verify.yml`.

Verification:

- Contract: start limit in `[Unit]` of all three renderings, sized against restart and
  start timeouts. Mode: focused-test.
  `tests/deploy/test_provider_authority_local_provisioning.py::test_authority_units_bound_their_restart_loop`
  (parametrized over the three paths); red: `StartLimitBurst` missing from `[Unit]`; green:
  `uv run python -m pytest <that id> -q`.
- Contract: the carrier's drift proof can restart a unit it drove into start-limit-hit.
  Mode: focused-test.
  `tests/deploy/test_provider_authority_local_provisioning.py::test_drift_proof_resets_the_start_limit_before_restoring`;
  red: the task before `Restore authority readiness after drift proofs` is not a
  `systemctl reset-failed kdive-external-boot-authority.service` command; green: same
  pytest id. Live behaviour is not run (no lab grant).

Steps:

1. Write the test: split each file's text on `[Service]`, parse `Key=Value` lines of each
   half; require `StartLimitIntervalSec=600` and `StartLimitBurst=5` in the unit half and
   absent from the service half, and
   `burst * (TimeoutStartSec + RestartSec) < interval` with seconds parsed from the `90s`
   and `5s` values. Run: red.
2. Add after the `Wants=` line of `[Unit]` in each of
   `deploy/ansible/roles/provider_authority_host/templates/authority.service.j2`,
   `deploy/ansible/roles/live_vm_host/templates/external-boot-authority.service.j2` and
   `deploy/systemd/system/kdive-external-boot-authority.service`:

   ```ini
   # Five failed starts in ten minutes leave the unit failed instead of restarting forever.
   StartLimitIntervalSec=600
   StartLimitBurst=5
   ```

3. Write the drift-proof test: load `verify.yml`, find the block named `Prove authority
   readiness retracts on credential and ACL drift`, and require the `always` task directly
   before `Restore authority readiness after drift proofs` to be an `ansible.builtin.command`
   with argv `[systemctl, reset-failed, kdive-external-boot-authority.service]` and
   `changed_when: false`. Run: red. Add that task, named `Clear the start limit the drift proof
   tripped`, in that position.
4. Run both tests green and `uv run python -m pytest tests/deploy/test_systemd_units.py -q`;
   commit `fix(deploy): bound the external-boot authority restart loop`.

## Rollback

Revert the two commits; no persisted state, migration or installed artifact outlives a
re-provision.
