# `faultinject.inspect` implementation plan (#3683)

Goal: add the read-only MCP tool `faultinject.inspect` for ready local-libvirt Systems and prove it live.
Architecture: an optional `fault_injection` port on `ProviderRuntime` (ADR-0825) implemented by a
new local-libvirt module that SSH-execs `/usr/local/sbin/kdive-faultinject read`; a System-keyed
MCP handler resolves role, state, endpoint and key, then calls the port off the event loop.
Tech stack: Python 3.14, FastMCP, psycopg pool, pytest; `just` recipes.
Expected implementation size: 900–1100 changed lines (L) — source ~350 (port 60, local module 130, tool 150, wiring 10), tests ~350, generated/docs/obligations ~250, live cell ~100.

## Global Constraints

- Ruff line length 100; `just lint`, `just type` (whole tree) green; no `KDIVE_*` env reads outside `kdive.config`.
- The wrapper docstring and `Field` text are the agent-facing contract; `suggested_next_actions` names only existing tools.
- Error categories come from `ErrorCategory`; remediation text is a fixed template (ADR-0123: no guest text, hostnames or key paths).
- Doc prose is plain; "Milestone", never "Sprint".
- Provisioning parity: this change adds no host tool or package.

## File map

- Create `src/kdive/providers/ports/fault_injection.py` — port, `FaultKnobState`, `FAULT_KNOB_PARAMETERS`.
- Modify `src/kdive/providers/core/runtime.py` — add `fault_injection: FaultInjectionInspector | None = None`.
- Create `src/kdive/providers/local_libvirt/debug/fault_inject.py` — `LocalLibvirtFaultInjection`, `parse_helper_state`, `_real_run_helper`.
- Modify `src/kdive/providers/local_libvirt/debug/live_introspect.py` — `_live_ssh_argv(..., program=_DRGN_HELPER)`.
- Modify `src/kdive/providers/local_libvirt/composition.py` — wire `fault_injection=`.
- Create `src/kdive/mcp/tools/debug/faultinject.py` — `inspect_faults` handler and `register`.
- Modify `src/kdive/mcp/assembly/tool_registration.py` — register it.
- Modify `src/kdive/mcp/exposure.py` (`_VIEWER`), `src/kdive/mcp/schema/tool_index.py`, `src/kdive/cli/commands/_generated_verbs.py` (regenerated), `docs/guide/reference/*`, `docs/guide/toolsets/debug.md`, `docs/guide/safety-and-rbac.md`, `src/kdive/mcp/resources/_content/*` (regenerated or mirrored), `scripts/coverage_campaign/obligations.toml`.
- Tests: `tests/providers/local_libvirt/test_fault_inject.py`, `tests/mcp/debug/test_faultinject_inspect.py`, `tests/mcp/core/test_tool_docs.py`, `tests/mcp/core/test_exposure.py` (if a list needs it), `tests/integration/live_stack/` cell, composition test assertion.

## Task 1 — Port and local-libvirt implementation

Interfaces. Consumes `TransportHandleData`, `run_ssh_with_retry`, `ssh_failure_details`, `Redactor`, `CategorizedError`. Provides:
`FaultKnobState(present: bool, parameters: Mapping[str, int])` with `.armed -> bool`; `FAULT_KNOB_PARAMETERS: Mapping[str, tuple[str, ...]]`;
`FaultInjectionInspector.read_state(self, *, transport_handle: str, key_path: str) -> dict[str, FaultKnobState]`;
`LocalLibvirtFaultInjection(secret_registry, run_helper=None)` with `from_env(*, secret_registry)`.
The seam `run_helper(transport_handle, key_path) -> tuple[int, bytes]` returns `(exit_code, stdout_capped_to_cap_plus_one)` and raises `CategorizedError(TRANSPORT_FAILURE)` for ssh exit 255 or a timeout.

Verification:
- Contract: reply parsing and validation. Mode: focused-test. `test_fault_inject.py::test_parse_*`; red: ImportError; green: `just test-verbose tests/providers/local_libvirt/test_fault_inject.py`. Cases: both knobs present; one absent; unknown knob key; missing parameter; bool or string value; extra top-level key; invalid JSON; over 4096 bytes.
- Contract: exit-code mapping. Mode: focused-test, same file: exit 3/4/127 give the three `configuration_error` reasons, 255 `transport_failure`, 2/5/other `infrastructure_failure`; `armed` truth table (probability 0, times 0, times -1).
- Contract: redaction. Mode: focused-test: a registered secret in stdout is masked before parsing (reply then fails shape validation).
- Contract: argv. Mode: focused-test in `test_introspect_drgn.py` (default program unchanged) and `test_fault_inject.py` (argv ends `-- /usr/local/sbin/kdive-faultinject read`, non-loopback handle refused).

Steps: write the tests, run red, implement the port module, the runtime field, the local module and the `program` parameter, run green, `just lint`, `just type`, commit `feat(providers): add the fault-injection inspector port (#3683)`.

## Task 2 — MCP handler, registration, composition

Interfaces. Consumes Task 1 port; `SYSTEMS.get`, `require_role`, `authority_ssh_forward_port`, `resolver.binding_for_system`, `connector.recorded_ssh_endpoint`, `load_system_bootstrap_private_key`, `materialized_private_key`, `capability_unsupported`. Provides `inspect_faults(pool, ctx, system_id, *, resolver, secret_registry) -> ToolResponse` and `register(app, pool, *, resolver, secret_registry)`.

Verification:
- Contract: handler behavior and error mapping. Mode: focused-test `tests/mcp/debug/test_faultinject_inspect.py`; red: ImportError; green: `just test-verbose tests/mcp/debug/test_faultinject_inspect.py`. Cases: success shape; invalid id; not found; foreign project; viewer allowed and a no-role caller denied; not ready; provider without port; no SSH forward; each port error propagates as its category and `data.reason`; the port runs via `asyncio.to_thread` with the key file removed after the call.
- Contract: composition wiring. Mode: focused-test: extend the existing local-libvirt composition test to assert `runtime.fault_injection` is set and other providers' is `None`.
- Contract: registration. Mode: focused-test `tests/mcp/core/test_app.py` tool list and `test_tool_docs.py`.

Steps: tests red, implement handler and registrar, wire composition and `tool_registration.py`, green, lint, type, commit `feat(mcp): add faultinject.inspect (#3683)`.

## Task 3 — Classification, docs, generated surfaces

Mode and contracts: exposure `_VIEWER`, tool index keywords, `_BEHAVIOR_TESTS_BY_TOOL` entry, reference docs, toolset, safety-and-rbac row, resources mirror, generated CLI verbs, obligations rows. Mode: focused-test: `just test-verbose tests/mcp/core/test_exposure.py tests/mcp/core/test_tool_docs.py tests/mcp/test_tool_index.py tests/guards`; red: the guards fail until each entry exists; plus the generator check recipes (`just docs-check`, `just cli-verbs-check` or the repository's equivalent, `just coverage-check`), run after regeneration. Commit `feat(mcp): classify and document faultinject.inspect (#3683)`.

## Task 4 — Live cell and proof

Contract: live_stack tool cell `tool/faultinject.inspect/default/{authentication,authorization,functional,project-isolation,validation}` bound in `tests/integration/live_stack/`, additive only. Mode: task-test-not-applicable for the cell source until run: it executes only on a live stack, so the focused observation is the live run itself; the structural binding is checked by `tests/integration/live_stack/test_tool_cells.py` in the ordinary suite. Live steps: rebuild a debug image with the helper, prove the image and both venvs carry the branch (grep a symbol only this branch adds), bring up the stack through `demo-up.sh`, provision a System, run the cell, then show the configuration_error path (guest without debugfs mounted or helper removed), tear down, record redacted evidence on the issue via `--body-file`.

## Self-review

Every Success item maps to a task: 1 to Tasks 1-2, 2 to Task 1, 3 to Task 2-3, 4 to Task 1, 5 to Task 3, 6 to Task 4. Names `read_state`, `FaultKnobState`, `inspect_faults`, `register` are used consistently. Borrowed names verified in source: `authority_ssh_forward_port`, `binding_for_system`, `recorded_ssh_endpoint`, `load_system_bootstrap_private_key`, `materialized_private_key`, `capability_unsupported`, `run_ssh_with_retry`, `ssh_failure_details`, `TransportHandleData`.
