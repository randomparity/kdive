# Distinguish a missing domain from a missing SSH forward (#2502)

**Goal.** `recorded_ssh_endpoint` on the local-libvirt connector maps *every* `CONFIGURATION_ERROR`
from the SSH endpoint resolver to `None`. The resolver raises two, with distinct messages. Tag each
raise with a `reason` detail and narrow the `except` to the no-forward reason, so the no-domain
error propagates through the five existing call sites — which already funnel a `CategorizedError`
through `failure_from_error` — and names its own condition.

**Tech stack.** Python (`requires-python = "==3.14.*"`, ruff `target-version = "py314"`), `uv`,
pytest, ruff, mypy.
[ADR-0658](../../adr/0658-missing-domain-is-not-a-missing-ssh-forward.md) ·
[spec](../specs/2026-09-15-ssh-reason-distinguish-design.md).
Expected implementation size: 90–140 changed lines (S) — from the file map below: one connector
module, four docstring sites, one regenerated reference, new cases in two test modules.

## Global Constraints

- The new reason value is exactly `system_domain_not_found`.
- `reason="ssh_not_provisioned"` and `_UNPROVISIONED_DETAIL` in `ssh_access.py` are unchanged byte
  for byte; the assertions pinning them in `tests/mcp/lifecycle/test_systems_ssh_access.py`,
  `tests/jobs/handlers/test_ssh_authorize.py`, and `tests/jobs/handlers/test_ssh_reachable.py` pass
  **unedited**.
- Exactly one existing test changes: `test_recorded_ssh_endpoint_none_when_not_provisioned`
  (`tests/providers/local_libvirt/test_connect.py:746`) raises a **bare** `CONFIGURATION_ERROR` and
  asserts `None`, pinning the collapse this issue calls a defect. Verified: it is the only bare
  `CONFIGURATION_ERROR` reaching `recorded_ssh_endpoint` in that module.
- Do not touch the gdbstub `_config_error` raise sites in the same module (approved exclusion,
  #2502), or `recorded_ssh_endpoint` in remote-libvirt or fault-inject.
- `docs/guide/reference/systems.md` is generated — never hand-edit; run `just docs`.
  `docs/adr/README.md` has no row table; add no index row.
- Guardrails: `just lint`, `just type`, `just test-changed`, `just docs-check`.

## File map

| Path | Owns now | Owns after |
|---|---|---|
| `providers/local_libvirt/lifecycle/connect.py` | `_config_error` (message only); two SSH raise sites; a broad `except` | a `reason` detail on both raises; `except` narrowed to the no-forward reason |
| `providers/ports/lifecycle.py`, `jobs/handlers/connectivity/ssh_{authorize,reachable}.py`, `mcp/tools/lifecycle/systems/registrar.py` | contract prose naming only `ssh_not_provisioned` | same, plus `system_domain_not_found` |
| `docs/guide/reference/systems.md` | generated from the registrar docstrings | regenerated |
| `tests/providers/local_libvirt/test_connect.py`, `tests/mcp/lifecycle/test_systems_ssh_access.py` | resolver/connector cases; pinned `ssh_not_provisioned` cases | plus one case per condition, and no-domain cases for both tools |

## Task 1 — Tag the two SSH raise sites and narrow the swallow

Modifies `src/kdive/providers/local_libvirt/lifecycle/connect.py` and
`src/kdive/providers/ports/lifecycle.py`. Tests `tests/providers/local_libvirt/test_connect.py`.

**Interfaces.** Consumes nothing. Defines for Task 2 and the tests:
`_config_error(message: str, *, reason: str | None = None) -> CategorizedError`, and constants
`_REASON_NO_DOMAIN = "system_domain_not_found"`, `_REASON_NO_FORWARD = "ssh_not_provisioned"`.
`recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None` is unchanged.

**Verification.**

- Contract: `recorded_ssh_endpoint` returns `None` only for the no-forward reason. Mode:
  focused-test — two cases via the existing helper `_recorded_endpoint_connector(resolve_ssh_endpoint)`
  (line 732). Red: the no-domain case fails `Failed: DID NOT RAISE <class
  'kdive.domain.errors.CategorizedError'>`. Green:
  `just test-verbose tests/providers/local_libvirt/test_connect.py`.
- Contract: each raise carries its own `reason` detail. Mode: focused-test — same file, asserting
  `details["reason"]` in the existing raise-site tests at lines 664 and 673. Same green command.

**Steps.**

1. Write the two connector cases with `_recorded_endpoint_connector`: one injecting a function that
   raises the tagged no-forward error, asserting `is None`; one injecting the tagged no-domain
   error, asserting it propagates with
   `excinfo.value.details["reason"] == "system_domain_not_found"`. Run the green command; expect
   the red named above.
2. Widen `_config_error` (line 75) to the signature in Interfaces above: it passes
   `details={"reason": reason}` when `reason` is given and `details={}` otherwise, keeping the
   category it already sets. The parameter stays optional so the excluded gdbstub call sites, which
   share this helper, need no edit.
3. Add the two constants beside the module's other module-level constants, with a comment naming
   #2502 and ADR-0658 as why the two are kept apart.
4. Tag the no-domain raise in `_resolve_ssh_endpoint_via`'s `resolve` with `reason=_REASON_NO_DOMAIN`,
   replacing its message with one naming the check #2480 needed: `f"System {domain_name!r} has no
   libvirt domain on this connection; check that the System is running and that this process and
   the worker that provisioned it read the same libvirt endpoint"`. No host path or libvirt URI
   enters the message.
5. Tag the no-forward raise in `_resolved_ssh_port` with `reason=_REASON_NO_FORWARD`, leaving its
   message exactly as it is.
6. Narrow the `except` in `recorded_ssh_endpoint`: replace
   `if exc.category is ErrorCategory.CONFIGURATION_ERROR` with
   `if exc.details.get("reason") == _REASON_NO_FORWARD`. Rewrite the docstring to say `None` means
   exactly one thing — the domain was read and records no forward (ADR-0271 narrowed by ADR-0658) —
   while a missing domain propagates as `system_domain_not_found`, the collapse that misdiagnosed
   #2480.
7. Update the test at line 746: add `details={"reason": "ssh_not_provisioned"}` to the
   `CategorizedError` its `_raise` helper builds. Add one `details["reason"]` assertion each to the
   raise-site tests at lines 664 and 673. Rerun the green command; expect every case green.
8. Update the `recorded_ssh_endpoint` docstring in `providers/ports/lifecycle.py`: `None` means no
   recorded SSH forward, and an implementation may raise `CONFIGURATION_ERROR` for a condition that
   is not that. Keep the existing `Raises:` entry.
9. Run `just lint` and `just type`; expect exit 0, no warnings. Commit
   `fix(providers): distinguish a missing domain from a missing SSH forward`.

**Acceptance.** `recorded_ssh_endpoint` returns `None` only for the no-forward reason; the no-domain
error propagates with `details["reason"] == "system_domain_not_found"` and a message naming the
endpoint check; every other case in the file passes unedited except the one named above.

## Task 2 — Carry the distinction into the client-facing contract text

Modifies the two connectivity job handlers, `registrar.py`, and the generated
`docs/guide/reference/systems.md`. Tests `tests/mcp/lifecycle/test_systems_ssh_access.py`.

**Interfaces.** Consumes Task 1's propagating `CategorizedError` carrying
`details["reason"] == "system_domain_not_found"`. Defines nothing; no call site changes, because
`ToolResponse.failure_from_error(object_id, exc)` already merges `safe_error_details(exc.details)`
into the response `data`.

**Verification.**

- Contract: `systems.ssh_info` returns `data.reason == "system_domain_not_found"` with
  `error_category == CONFIGURATION_ERROR` when the connector raises the no-domain error. Mode:
  focused-test. Red before Task 1 lands: the reason is absent. Green:
  `just test-verbose tests/mcp/lifecycle/test_systems_ssh_access.py`.
- Contract: `systems.check_ssh_reachable` likewise. Mode: focused-test — same file and command.
- Contract: the generated reference matches the registrar docstrings. Mode: focused-test — red
  `just docs-check` exits 1 with "tool reference is stale"; green after `just docs`.
- Contract: handler and port `Raises:` prose. Mode: task-test-not-applicable — prose describing
  behavior the focused tests already prove; no executable consumer parses it, and asserting on its
  wording would test the sentence rather than the contract.

**Steps.**

1. Extend the existing `_FakeConnector` (line 30, constructed `_FakeConnector(endpoint)`) with a
   keyword-only `raises: CategorizedError | None = None` that `recorded_ssh_endpoint` raises when
   set. Keep the positional argument and the `seen_handles` capture so every current construction
   site works unedited, and preserve the comment above `seen_handles.append` — it records a
   regression.
2. Add a module-level helper returning the no-domain `CategorizedError`: the Task 1 step 4 message,
   `category=ErrorCategory.CONFIGURATION_ERROR`, `details={"reason": "system_domain_not_found"}`.
3. Add the two cases from Verification, asserting
   `resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value` and
   `resp.data["reason"] == "system_domain_not_found"`. Do not edit the existing
   `ssh_not_provisioned` cases. Run the green command; expect new and pinned cases green.
4. In both job handlers, extend the existing `Raises:` entry with one clause naming the propagating
   `system_domain_not_found` beside `ssh_not_provisioned`. Change no code.
5. In `registrar.py`, extend the `ssh_info` and `check_ssh_reachable` docstrings: after the existing
   `ssh_not_provisioned` sentence, state that a System with no libvirt domain on the worker's
   connection reports `system_domain_not_found` instead — an endpoint or liveness fault, not a
   provisioning gap.
6. Run `just docs`, then `just docs-check`; expect exit 0 and a diff confined to the two tool
   sections edited in step 5.
7. Run `just lint`, `just type`, `just test-changed`; expect exit 0. Commit
   `docs(mcp): name the missing-domain reason in the SSH tool contract`.

**Acceptance.** Both MCP tools return the new reason for a no-domain connector error; the pinned
`ssh_not_provisioned` assertions pass unedited; `just docs-check` exits 0.
