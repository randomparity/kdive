# Authority retirement tolerates a missing account and retires the memlock ceiling (#2891)

## Scope and authority

Campaign scope for issue #2891, token `q2891-be130088`. Operator-approved exclusions: the
install-path restart in `provider_authority_host/tasks/libvirt.yml` (already correct);
`live_vm_host` / `local_worker_host` getent indexing (operator); account removal and linger
behaviour (operator). Decision: [ADR-0708](../../adr/0708-authority-retirement-restarts-the-user-manager.md).

## Problem

1. `authority_host_teardown.yml` looks the account up with `getent` and `fail_key: false`. The
   installed module sets a missing key to `None` (`ansible/modules/getent.py`, `rc == 2` branch),
   so `authority_account in ansible_facts.getent_passwd` is true and
   `ansible_facts.getent_passwd[authority_account][1]` fails. Five `when` guards and two assert
   clauses use that form.
2. Both retirement paths remove the memlock drop-in and only reload systemd; a running authority
   user manager keeps its unlimited ceiling.

## Design

- **Guard form.** Every teardown task that indexes the passwd entry uses
  `ansible_facts.getent_passwd[authority_account] | default(none) is not none`; the two assert
  clauses use `... | default(none) is none or ...`. No `in`/`not in` passwd test remains.
- **Ceiling retirement (ADR-0708).** Four tasks, the same in both files, guarded by the
  known-account form (in `disable.yml`, keyed on `'kdive-provider-authority'`):
  1. read `MainPID` of `user@<uid>.service`;
  2. read its configured `LimitMEMLOCK`;
  3. when the PID is not `0`, read the running hard limit with `prlimit`;
  4. restart `user@<uid>.service` when the PID is not `0`, the running limit is `unlimited`, and
     the configured limit is not `infinity`.
- **Order.** Teardown: after the service/process assertions, the drop-in removal, both reloads,
  and the LOGIN revocation. Role: after the drop-in removal, an unconditional system reload (a re-run whose removal
  reports `ok` notifies no handler, and `systemctl show` reports the loaded limit until a reload),
  then the four tasks, then the unchanged linger task and handler flush.
- **Runbook.** The retirement paragraph states that teardown tolerates an absent account and
  restarts a user manager still holding the retired ceiling.

## Failure model

1. **Actors and deployments** — an operator running the teardown playbook or the host play with
   the authority disabled, on a self-hosted KVM runner (x86_64 or ppc64le).
2. **Invariants and assets at stake** — no authority guest or daemon runs after retirement; the
   fixed-worker libvirt path stays usable; retirement never starts a manager that was not running.
3. **Accepted failure classes** — an account present with no running manager: user-scope tasks
   already assume a running manager (unchanged, pre-existing). A host that grants the manager
   `infinity` by its own configuration keeps it, and one that grants it through `pam_limits`
   restarts it on every teardown run (ADR-0708 consequences).
4. **Covered elsewhere** — linger and account removal (operator exclusion), including the state
   where the account is gone but its private unit file remains, which still fails the role's
   existing `getent` in the endpoint-stop block; install-path restart
   (`libvirt.yml`, #2886).

## Success

- Teardown on a host with no authority account completes (live arm 2).
- On a host with the drop-in and an unlimited manager, one retirement leaves the manager at the
  host default and a second run restarts nothing (live arms 1 and 3).
- The fixed-worker `virsh list` check passes (live arm 4).

## Validation

- `focused-test`: `tests/deploy/test_live_worker_provisioning.py` renders every teardown `when`
  that indexes the passwd entry with Jinja against a `None` entry (false, no error) and a real
  entry (true); red on today's `in` guards.
- `focused-test`: both test files assert the four tasks' commands, conditions, and order.
- `task-test-not-applicable`: runbook prose — no executable consumer; `just docs-links` and
  `just docs-paths` cover its links.
- Live: the operator-approved four arms on a native ppc64le (POWER9) KVM host, then reinstall.
