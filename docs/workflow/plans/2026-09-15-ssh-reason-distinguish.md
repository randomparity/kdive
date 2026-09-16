# Distinguish a missing domain from a missing SSH forward (#2502)

**Goal.** `recorded_ssh_endpoint` on the local-libvirt connector maps *every* `CONFIGURATION_ERROR`
from the SSH endpoint resolver to `None`, though the resolver raises two with distinct messages. Tag
each raise with a `reason` and narrow the `except` to the no-forward one, so the no-domain error
propagates through the existing call sites and names its own condition.

**Tech stack.** Python (`requires-python = "==3.14.*"`, ruff `target-version = "py314"`), `uv`,
pytest, ruff, mypy. [ADR-0658](../../adr/0658-missing-domain-is-not-a-missing-ssh-forward.md) ·
[spec](../specs/2026-09-15-ssh-reason-distinguish-design.md). Expected implementation size: 120–180
changed lines (S) — from the file map: one connector module, five docstring sites, one regenerated
reference, seven new cases across four test modules. Measured after the build: 295 changed lines,
with that task coverage unchanged. The estimate was low because each MCP-tool and job-handler case
carries async pool/seed/resolver fixture boilerplate (~14 lines, not ~8) and three test helpers
grew. Band stays S; scope, design, and task list are unchanged.

## Global Constraints

- The new reason value is exactly `system_domain_not_found`.
- **Three** MCP tools render this reason, not two: `systems.ssh_info`, `systems.authorize_ssh_key`
  (mutating, CONTRIBUTOR), `systems.check_ssh_reachable` — `ssh_access.py` lines 78, 156, 231.
- `reason="ssh_not_provisioned"` and `_UNPROVISIONED_DETAIL` in `ssh_access.py` are unchanged byte
  for byte; the assertions pinning them in `tests/mcp/lifecycle/test_systems_ssh_access.py` and
  `tests/jobs/handlers/test_ssh_{authorize,reachable}.py` pass **unedited**.
- Exactly one existing test changes: `test_recorded_ssh_endpoint_none_when_not_provisioned`
  (`tests/providers/local_libvirt/test_connect.py:746`) raises a **bare** `CONFIGURATION_ERROR` and
  asserts `None`, pinning the collapse this issue calls a defect. Verified: the only such bare raise
  reaching `recorded_ssh_endpoint`.
- Do not touch the gdbstub `_config_error` raise sites in the same module (approved exclusion,
  #2502), or `recorded_ssh_endpoint` in remote-libvirt or fault-inject.
- `docs/guide/reference/systems.md` is generated — never hand-edit; run `just docs`.
  `docs/adr/README.md` has no row table; add no index row.
- Guardrails, all part of `just ci`: `just lint`, `just type`, `just test-changed`,
  `just docs-check`, `just docs-links`, `just docs-paths`, `just records`. `docs-paths` resolves
  every concrete `docs/<path>` reference in changed markdown, so a glob or a truncated path in a
  plan or spec fails it — write whole filenames.

## File map

| Path | Owns now | Owns after |
|---|---|---|
| `providers/local_libvirt/lifecycle/connect.py` | `_config_error` (message only); two SSH raise sites; a broad `except` | a `reason` detail on both raises; `except` narrowed to the no-forward reason |
| `providers/ports/lifecycle.py`, `jobs/handlers/connectivity/ssh_{authorize,reachable}.py`, `mcp/tools/lifecycle/systems/registrar.py` | contract prose naming only `ssh_not_provisioned` | same, plus `system_domain_not_found` |
| `docs/guide/reference/systems.md`; `docs/operating/runbooks/live-stack.md`; `docs/adr/0298-ssh-reachable-runtime-probe.md` | generated reference; a runbook naming the old reason for this exact symptom; the `None` contract this narrows | regenerated; corrected reason; amendment banner under `## Status` |
| `tests/providers/local_libvirt/test_connect.py`, `tests/mcp/lifecycle/test_systems_ssh_access.py`, `tests/jobs/handlers/test_ssh_{authorize,reachable}.py` | resolver/connector cases; pinned `ssh_not_provisioned` cases | plus one case per condition, no-domain cases for all three tools, one propagation case per handler |

## Task 1 — Tag the two SSH raise sites and narrow the swallow

Modifies `providers/local_libvirt/lifecycle/connect.py` and `providers/ports/lifecycle.py`. Tests
`tests/providers/local_libvirt/test_connect.py`.

**Interfaces.** Consumes nothing. Defines for Task 2 and the tests:
`_config_error(message: str, *, reason: str | None = None) -> CategorizedError`, and constants
`_REASON_NO_DOMAIN = "system_domain_not_found"`, `_REASON_NO_FORWARD = "ssh_not_provisioned"`.
`recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None` is unchanged.

**Verification.** Each entry below is also this task's acceptance criterion.

- Contract: `recorded_ssh_endpoint` returns `None` only for the no-forward reason. focused-test —
  two cases via the existing helper `_recorded_endpoint_connector(resolve_ssh_endpoint)` (line 732).
  Red: the no-domain case fails `DID NOT RAISE ... CategorizedError`. Green:
  `just test-verbose tests/providers/local_libvirt/test_connect.py`.
  The same file and command also pin each raise's own `reason`, via `details["reason"]`
  assertions added to the existing raise-site tests at lines 664 and 673.

**Steps.**

1. Write the two connector cases with `_recorded_endpoint_connector`: one injecting a function
   raising the tagged no-forward error, asserting `is None`; one injecting the tagged no-domain
   error, asserting `excinfo.value.details["reason"] == "system_domain_not_found"`. Expect the red above.
2. Widen `_config_error` (line 75) to the Interfaces signature: pass `details={"reason": reason}`
   when given, `details={}` otherwise, keeping its category. The parameter stays optional so the
   excluded gdbstub call sites sharing this helper need no edit. Add the two constants beside the
   module's other module-level constants, commented with #2502 and ADR-0658.
3. Tag the no-domain raise in `_resolve_ssh_endpoint_via`'s `resolve` with `reason=_REASON_NO_DOMAIN`,
   replacing its message with `f"System {domain_name!r} has no libvirt domain on this connection;
   check that the System is running and that this process and the worker that provisioned it read
   the same libvirt endpoint"`. No host path or libvirt URI enters the message.
4. Tag the no-forward raise in `_resolved_ssh_port` with `reason=_REASON_NO_FORWARD`, leaving its
   message exactly as it is.
5. Narrow the `except` in `recorded_ssh_endpoint`: keep the
   `exc.category is ErrorCategory.CONFIGURATION_ERROR` test and conjoin
   `exc.details.get("reason") == _REASON_NO_FORWARD`. Both conjuncts are load-bearing — the reason
   narrows to the absent forward, and the category keeps the predicate no wider than the contract.
   Rewrite the docstring to say `None` means
   exactly one thing — the domain was read and records no forward (ADR-0298, narrowed by ADR-0658)
   — while a missing domain propagates as `system_domain_not_found`.
6. Update the test at line 746: add `details={"reason": "ssh_not_provisioned"}` to the
   `CategorizedError` its `_raise` helper builds. Add one `details["reason"]` assertion each to the
   raise-site tests at lines 664 and 673. Rerun the green command; expect every case green.
7. Update the `recorded_ssh_endpoint` docstring in `providers/ports/lifecycle.py`: `None` means no
   recorded SSH forward, and an implementation may raise `CONFIGURATION_ERROR` for a condition that
   is not that. Keep the existing `Raises:` entry.
8. Run `just lint`, `just type`, and `just docs-paths`; expect exit 0, no warnings. Commit
   `fix(providers): distinguish a missing domain from a missing SSH forward`.


## Task 2 — Carry the distinction into the client-facing contract text

Modifies the two connectivity job handlers, `registrar.py`, and the generated
`docs/guide/reference/systems.md`. Tests `test_systems_ssh_access.py`, `test_ssh_{authorize,reachable}.py`.

**Interfaces.** Consumes Task 1's propagating `CategorizedError` carrying
`details["reason"] == "system_domain_not_found"`. Defines nothing; no call site changes, because
`ToolResponse.failure_from_error(object_id, exc)` already merges `safe_error_details(exc.details)`
into `data` — and the worker prefixes job detail keys to `failure_detail_reason`.

**Verification.** Each entry below is also this task's acceptance criterion.

- Contract: each of the three MCP tools returns `data.reason == "system_domain_not_found"` with
  `error_category == CONFIGURATION_ERROR` when the connector raises the no-domain error.
  focused-test — three cases. Red before Task 1 lands: the reason is absent. Green:
  `just test-verbose tests/mcp/lifecycle/test_systems_ssh_access.py`.
- Contract: both job handlers propagate rather than collapse. focused-test — one case each in
  `test_ssh_authorize.py` and `test_ssh_reachable.py` asserting the raised `CategorizedError`
  carries `details["reason"] == "system_domain_not_found"`. Green:
  `just test-verbose tests/jobs/handlers/test_ssh_authorize.py`.
- Contract: the generated reference matches the registrar docstrings and cross-references resolve.
  focused-test — red `just docs-check` exits 1 with "tool reference is stale"; green after
  `just docs`, then `just docs-links` exits 0.
- Contract: handler and port `Raises:` prose. task-test-not-applicable — prose describing behavior
  the focused tests prove; no executable consumer parses it, and asserting on its wording would
  test the sentence rather than the contract.

**Steps.**

1. Extend the existing `_FakeConnector` (line 30, constructed `_FakeConnector(endpoint)`) with a
   keyword-only `raises: CategorizedError | None = None` that `recorded_ssh_endpoint` raises when
   set. Keep the positional argument and the `seen_handles` capture so every current construction
   site works unedited, and preserve the comment above `seen_handles.append` — it records a regression.
2. Add a module-level helper returning the no-domain `CategorizedError`: the Task 1 step 4 message,
   `category=ErrorCategory.CONFIGURATION_ERROR`, `details={"reason": "system_domain_not_found"}`.
   Add the three tool cases, asserting
   `resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value` and
   `resp.data["reason"] == "system_domain_not_found"`. `authorize_ssh_key` is mutating and
   CONTRIBUTOR-gated: seed its case like the existing ones here and pass a valid public key so the
   run reaches the connector. Do not edit the existing `ssh_not_provisioned` cases.
3. Add one case each to the two job-handler test modules, modelled on their existing
   `ssh_not_provisioned` case: a connector raising the no-domain error, asserting the handler
   propagates it with `details["reason"] == "system_domain_not_found"`.
4. In both job handlers, extend the existing `Raises:` entry with one clause naming the propagating
   `system_domain_not_found` beside `ssh_not_provisioned`, and note the `failure_detail_` prefix.
   Change no code.
5. In `registrar.py`, add to the `ssh_info` and `check_ssh_reachable` docstrings, after their
   existing `ssh_not_provisioned` sentence, that a **local-libvirt** System with no libvirt domain
   on the worker's connection reports `system_domain_not_found` — an endpoint or liveness fault,
   not a provisioning gap. Qualify it to local-libvirt: remote-libvirt still returns `None` for an
   absent domain (`remote_libvirt/lifecycle/connect.py:163-166`), so an unqualified sentence would
   be false there. `authorize_ssh_key`'s docstring (line 361) has **no** `ssh_not_provisioned`
   sentence to extend — add one short sentence carrying both reasons instead.
6. Correct the two records this change invalidates. `docs/operating/runbooks/live-stack.md:85-86`
   says the split-endpoint symptom surfaces as `ssh_not_provisioned`; it now surfaces as
   `system_domain_not_found` — update that sentence only. Then add an amendment banner under
   `## Status` in `docs/adr/0298-ssh-reachable-runtime-probe.md`, copying the blockquote form
   `docs/adr/0574-systemd-supervises-host-worker-incarnations.md` uses: bolded
   "Partially superseded by" plus a relative markdown link whose
   text is the number 0658 and whose target is this change's ADR filename, then `(2026-09-15):` and
   one line saying `None` now means a read domain recording no forward. Nothing else there changes.
7. Run `just docs`, then `just docs-check`; expect exit 0 and a diff confined to the three tool
   sections edited in step 5.
8. Run `just lint`, `just type`, `just test-changed`, `just docs-links`; expect exit 0. Commit
   `docs(mcp): name the missing-domain reason in the SSH tool contract`.
