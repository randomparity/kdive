# Installed authority lane bring-up: implementation plan (#3215)

**Goal:** an installed local-authority lane runs the authority System frame after a standard
bring-up, with no manual route exports, no hand-written harness DSN and no ad hoc inventory.

**Architecture:** the `live_vm_host` role publishes the five fixed-worker route values in
`/etc/kdive/live-worker-authority.env`; `worker-lifecycle.sh start` forwards them. `env.sh`
derives client endpoints from `ADDR:PORT` port values. The frame refuses an all-routed lane
inventory before mutation, and the runbook documents the fixture-mode inventory. Spec:
`docs/workflow/specs/2026-10-13-authority-lane-bring-up-design.md`.

**Tech stack:** bash 5, Ansible (ansible-core 2.21.1), Python 3.14, pytest.

Expected implementation size: 230–300 changed lines (M) — summed from the file map: about 45
script lines, 40 role/teardown lines, 15 frame lines, 15 registry/doc-reference lines, 40
runbook lines and 110 test lines.

## Global Constraints

- No change to the lifecycle request schema, `src/kdive` product behaviour, or any migration.
- New shell names under `scripts/` carry no `KDIVE_` prefix (`AUTHORITY_ROUTE_ENV`,
  `AUTHORITY_ROUTE_NAMES`, `backend_endpoint`); only the five existing route names keep it.
- Today's default URLs for a bare `PORT` value stay byte-identical.
- Doc style: plain prose; no "critical", "robust", "comprehensive", "elegant".
- The tool shell is zsh: run multi-line checks under `bash`. Run gates bare.

## File map

| File | Change |
|------|--------|
| `scripts/live-stack/worker-lifecycle.sh` | add `load_published_authority_route`; call it in `request start` |
| `scripts/live-stack/env.sh` | add `backend_endpoint`; derive Postgres/SeaweedFS/OIDC defaults |
| `deploy/ansible/roles/live_vm_host/tasks/main.yml` | publish or withdraw the route file |
| `deploy/ansible/roles/live_vm_host/tasks/verify.yml` | verify it |
| `deploy/ansible/playbooks/authority_host_teardown.yml` | remove it and assert absence |
| `tests/integration/live_stack/authority_lane.py` | all-routed inventory stops `blocked` in `select_lane` |
| `src/kdive/config/external_env.py`, `docs/guide/reference/config.md` | port descriptions (regenerated) |
| `docs/operating/runbooks/live-testing.md`, `self-hosted-kvm-runner.md`, `scripts/live-stack/README.md` | lane bring-up text |
| `docs/adr/0770-installed-worker-authority-route-is-published.md` | the decision |
| tests: `tests/scripts/test_live_stack_scripts.py`, `tests/deploy/test_live_worker_provisioning.py`, `tests/integration/live_stack/test_authority_lane.py` | per task |

## Task 1 — route forwarding in `worker-lifecycle.sh`

**Interfaces:** consumes `require_exact_file FILE MODE` (existing, same file). Provides
`load_published_authority_route` (no args; exports into the caller; returns 1 with a stderr
message on failure). Seam: `AUTHORITY_ROUTE_ENV` (default `/etc/kdive/live-worker-authority.env`).

**Verification:**
- Contract: forwarding and refusal. `Mode: focused-test` —
  `tests/scripts/test_live_stack_scripts.py::test_authority_route_*`. Red: the function is
  undefined (`command not found`, exit 127). Green:
  `just test-verbose tests/scripts/test_live_stack_scripts.py -k authority_route`.
- Contract: the start request carries the published route. `Mode: focused-test` —
  `test_lifecycle_start_forwards_the_published_authority_route` reuses
  `_lifecycle_start_with_inventory`'s pattern with a probe that prints the five request settings.
  Red: the probe shows `None` for each. Green: same command.

Steps:
1. Write the tests. The `stat` shim from `_published_contract` answers `0:0:644`. Cases:
   absent file → rc 0 and nothing exported; valid file → five exports; a mode answer other than
   `0:0:644` → rc 1; six lines, a duplicate, an unknown name, an empty value, or a line with no
   `=` → rc 1 with "exactly the five"; a preset differing from the file → rc 1 naming the
   variable; an equal preset → rc 0; the value `$(touch CANARY)` stays literal and the canary
   does not exist.
2. Run them and confirm red.
3. Add after `WORKER_EXECUTABLE`:

```bash
# Test-staging seam, not an operator knob (no KDIVE_ prefix): the root-published route file.
: "${AUTHORITY_ROUTE_ENV:=/etc/kdive/live-worker-authority.env}"
AUTHORITY_ROUTE_NAMES=(
  KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE
  KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET
  KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF
  KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF
  KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF
)

# Forward the installed fixed-worker authority route (ADR-0770, #3215). An absent file is a host
# without a worker authority client; a present one must be exact, and a contradicting preset is
# refused rather than honoured, because a worker on another route than the installed one fails
# only later, at the authority.
load_published_authority_route() {
  local line name value seen=" " invalid="${AUTHORITY_ROUTE_ENV} must contain exactly the five worker authority route assignments"
  local -a lines
  [[ -e "$AUTHORITY_ROUTE_ENV" || -L "$AUTHORITY_ROUTE_ENV" ]] || return 0
  require_exact_file "$AUTHORITY_ROUTE_ENV" 644 || return 1
  mapfile -t lines <"$AUTHORITY_ROUTE_ENV"
  ((${#lines[@]} == ${#AUTHORITY_ROUTE_NAMES[@]})) || { echo "$invalid" >&2; return 1; }
  for line in "${lines[@]}"; do
    name="${line%%=*}"
    value="${line#*=}"
    [[ $line == *=* && -n $value && " ${AUTHORITY_ROUTE_NAMES[*]} " == *" ${name} "* &&
      $seen != *" ${name} "* ]] || { echo "$invalid" >&2; return 1; }
    seen+="${name} "
    if [[ -n ${!name:-} && ${!name} != "$value" ]]; then
      echo "${name} contradicts the installed route in ${AUTHORITY_ROUTE_ENV};" \
        "unset it to use the installed route" >&2
      return 1
    fi
    export "${name}=${value}"
  done
}
```

4. In `request`, after `libvirt_uri="$(load_published_libvirt_uri)" || return 1`, add
   `load_published_authority_route || return 1`.
5. Confirm green; `just lint-shell`; commit `fix(live-stack): forward the installed worker authority route`.

## Task 2 — publish, verify and withdraw the route file

**Interfaces:** consumes the five `live_vm_host_worker_authority_*` defaults and
`live_vm_host_worker_authority_enabled`. Provides `/etc/kdive/live-worker-authority.env` in the
format Task 1 reads.

**Verification:**
- Contract: rendered content equals the Task 1 format. `Mode: focused-test` —
  `tests/deploy/test_live_worker_provisioning.py::test_worker_authority_route_file_is_published_and_withdrawn`
  renders the publish task's `content` with jinja2 (`StrictUndefined`) over the `live_vm_host`
  defaults plus `inventory_hostname: lane-host`, re-rendering until the text stops changing (the
  instance default nests two templates), and asserts the five lines in order with the INSTANCE
  line equal to `lane-host`, root:root `0644`, the `when` string on
  `live_vm_host_worker_authority_enabled` (defined in `local_worker_host` defaults, so asserted
  as text, not rendered), and a sibling `state: absent` task under the negated flag. Red: no task names the
  path. Green: `just test-verbose tests/deploy/test_live_worker_provisioning.py -k authority_route`.
- Contract: verify and teardown. `Mode: focused-test` — the same test asserts `verify.yml` checks
  mode `0644` and exact content, and that the teardown's removal and absence loops list the path.

Steps: write the test, see red; add after "Publish the selected worker libvirt URI":

```yaml
- name: Publish the fixed-worker authority route for stack bring-up
  ansible.builtin.copy:
    dest: /etc/kdive/live-worker-authority.env
    content: |
      KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE={{ live_vm_host_worker_authority_instance }}
      KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET={{ live_vm_host_worker_authority_request_socket }}
      KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF={{ live_vm_host_worker_authority_server_ca_ref }}
      KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF={{ live_vm_host_worker_authority_client_certificate_ref }}
      KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF={{ live_vm_host_worker_authority_client_key_ref }}
    owner: root
    group: root
    mode: "0644"
  when: live_vm_host_worker_authority_enabled | bool

- name: Withdraw the fixed-worker authority route when the client is disabled
  ansible.builtin.file:
    path: /etc/kdive/live-worker-authority.env
    state: absent
  when: not (live_vm_host_worker_authority_enabled | bool)
```

In `verify.yml`, after the libvirt URI assertions: `stat` the path; when enabled, assert
root/root/`0644`, `slurp` and assert the content equals the same five-line string built from the
same variables; when disabled, assert `not stat.exists`. In the teardown, add the path to the
"Remove authority unit and endpoint artifacts" and "Inspect removed authority endpoint
artifacts" loops. Then `just lint-ansible`, `just test-ansible`, green test; commit
`feat(ansible): publish the fixed-worker authority route`.

## Task 3 — `ADDR:PORT` backend endpoints in `env.sh`

**Interfaces:** provides `backend_endpoint NAME` → prints `host:port`, or returns 1 with a
message. Consumed only within `env.sh`.

**Verification:** `Mode: focused-test` — `test_backend_endpoint_*` and a rewritten
`test_client_urls_derive_from_the_configurable_ports` source `env.sh` via `_sourced` with
`KDIVE_LIBVIRT_URI=qemu:///system` and print the three derived URLs. Table: `5434` →
`localhost:5434`; `127.0.0.1:5434`, `0.0.0.0:5434`, `[::1]:5434` → `localhost:5434`;
`192.0.2.10:5434` → `192.0.2.10:5434`; `[2001:db8::1]:5434` and `2001:db8::1:5434` →
`[2001:db8::1]:5434`; `bad`, `1.2.3.4:0`, `1.2.3.4:70000`, `a b:5434`, `x/y:5434` → non-zero with
the variable named; and `KDIVE_OIDC_PORT=192.0.2.10:8090` gives the issuer
`http://192.0.2.10:8090/default`. Red: today's text gives `localhost:192.0.2.10:5434` and rc 0. Green:
`just test-verbose tests/scripts/test_live_stack_scripts.py -k "endpoint or client_urls"`.

Steps: write tests, see red; add before `database_host`:

```bash
# Client-facing host:port for a published backend port (#3215). The port variables also take
# compose's ADDR:PORT publish form. A loopback or wildcard address keeps the localhost every
# default URL used before (the OIDC issuer string is an identity), any other address becomes the
# URL host, and anything else stops the sourcing caller instead of producing a malformed URL.
backend_endpoint() {
  local name="$1" value="${!1}" addr="" port
  port="${value##*:}"
  [[ $value == *:* ]] && addr="${value%:*}"
  if [[ ! $port =~ ^[0-9]{1,5}$ ]] || ((10#$port < 1 || 10#$port > 65535)) ||
    [[ -n $addr && ! $addr =~ ^[][A-Za-z0-9.:-]+$ ]]; then
    echo "${name}=${value} must be PORT or ADDR:PORT with a port from 1 to 65535" >&2
    return 1
  fi
  case "$addr" in
  "" | 127.0.0.1 | 0.0.0.0 | ::1 | :: | "[::1]" | "[::]") addr=localhost ;;
  \[*\]) ;;
  *:*) addr="[${addr}]" ;;
  esac
  printf '%s:%s' "$addr" "$port"
}
postgres_endpoint="$(backend_endpoint KDIVE_POSTGRES_PORT)" || return $?
seaweedfs_endpoint="$(backend_endpoint KDIVE_SEAWEEDFS_PORT)" || return $?
oidc_endpoint="$(backend_endpoint KDIVE_OIDC_PORT)" || return $?
```

Replace `localhost:${KDIVE_POSTGRES_PORT}` with `${postgres_endpoint}`,
`http://localhost:${KDIVE_SEAWEEDFS_PORT}` with `http://${seaweedfs_endpoint}`, and both
`http://localhost:${KDIVE_OIDC_PORT}` with `http://${oidc_endpoint}`. Update the three
`ExternalEnvVar` descriptions to add "Accepts `PORT` or `ADDR:PORT`; a loopback or wildcard
address keeps `localhost` in the derived URL." Run `just config-docs`, `just config-docs-check`,
`just env-docs-check`, `just lint-shell`; commit `fix(live-stack): derive backend URLs from ADDR:PORT`.

## Task 4 — refuse an all-routed lane inventory before mutation

**Interfaces:** consumes `kdive.inventory.loader.load_inventory(path: Path) -> InventoryDoc`
and `InventoryDoc.local_libvirt[*].authority_instance`, and `kdive.inventory.errors.InventoryError`.
Provides constant `NO_PLAIN_BLOCKED`.

**Verification:** `Mode: focused-test` —
`test_all_routed_inventory_is_blocked_before_mutation` writes a `schema_version = 2` inventory
whose one `[[local_libvirt]]` carries `authority_instance` and asserts `ScenarioStop`
`BLOCKED`, the message names `authority_instance`, and `calls == []`;
`test_invalid_inventory_is_blocked_before_mutation` writes `schema_version = 1` and asserts the
same stop naming `KDIVE_SYSTEMS_TOML`. `_fake_lane` now writes a
plain-entry inventory, so the existing success test covers the proceed path. Red: the frame calls
`require_lane` first. Green:
`just test-verbose tests/integration/live_stack/test_authority_lane.py`.

Steps: write the test, see red; in `select_lane`, after the file check:

```python
    try:
        local = load_inventory(Path(inventory)).local_libvirt
    except InventoryError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{SYSTEMS_TOML_ENV} is invalid: {exc}") from exc
    if local and all(entry.authority_instance for entry in local):
        raise ScenarioStop(Outcome.BLOCKED, NO_PLAIN_BLOCKED)
```

with `NO_PLAIN_BLOCKED = "the lane inventory routes every [[local_libvirt]] entry through an
authority_instance; run fixture mode on the lane's fixture-mode inventory (live-testing
runbook)"`. `just lint`, `just type`; commit `test(live-stack): refuse an all-routed lane
inventory before mutation`.

## Task 5 — ADR and runbooks

**Verification:** `Mode: task-test-not-applicable` — prose for operators; no executable consumer
reads it. `just records`, `just lint` (ruff format covers Markdown Python fences) and the doc
checks in `just ci` gate shape and links.

Steps: write the ADR (Status Accepted, Context, Decision, Consequences, Considered & rejected).
In `live-testing.md`, under "Candidate evidence and cleanup": the bring-up forwards the route
from `/etc/kdive/live-worker-authority.env`, a contradicting export is refused, and `ADDR:PORT`
port values derive working default DSNs. Under the frame section step 2: the fixture-mode
inventory (a separate copy of the lane file with only the local entry's `authority_instance`
removed; the routed file is not edited), that the whole stack is brought up on that copy with the
harness exporting the same path and a `stack-services.sh` run switches back (only with no open
authority-owned System, job or recovery on the lane), why a plain resource
cannot sit beside the routed one, and the pre-mutation stop. In the proof command, use
`KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"` as it stands. In `self-hosted-kvm-runner.md`,
name the published file where it says refs are published. Add one sentence to
`scripts/live-stack/README.md`. Commit `docs: document the installed authority lane bring-up`.

## Live proof hand-off (separately approved)

Not a build task. After the PR's CI is green, a private plan held for root approval (naming its
SHA-256) runs on the lane: the stack stopped, the backends up, `installed-local-authority.yml` at
the candidate SHA with `live_vm_host_worker_authority_enabled` (its `verify.yml` must pass), the
route file read back root-side, then `stack-services.sh` on the fixture-mode copy with no route
exports and no explicit DSNs, `just onboard` separately with stdin from `/dev/null`, a second
`stack-services.sh` run (restart), and the frame command from `live-testing.md` with
`KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"`. Observed signals: the route preflight answers
`route-ok` after both bring-ups, the slot's worker environment carries the five values, the
harness DSN connects, and both frame tests pass with the before and after inventories equal.

## Rollback

Each task is one revertible commit. A lane converged with Task 2 keeps a root file that
pre-change checkouts ignore; disabling the worker client and converging removes it.
