# Authority-owned Systems in the SSH access tools — plan (#3541)

**Goal:** the three SSH tools and two connectivity handlers reach an authority-owned local-libvirt
System through its stored port.

**Architecture:** the server reads the port through `authority_ssh_forward_port`; the job payload
carries it to the worker; `recorded_ssh_endpoint(..., ssh_forward_port=)` builds the endpoint.
Spec: [design](../specs/2026-11-04-authority-ssh-access-3541-design.md).

**Stack:** Python 3.14, pydantic, psycopg, pytest with disposable Postgres.

Expected implementation size: 240–340 changed lines (M) — about 60 source lines over seven files plus the tests in the file map.

## Global Constraints

- No migration, no grant change, no new ADR, no new error reason.
- The worker never calls `authority_ssh_forward_port` or the binding function.
- Every connector takes `ssh_forward_port` keyword-only; only the local connector uses it.
- `ruff format` covers this file's Python fences. Run shell steps in bash.

## File map

| File | Change | Owns |
|---|---|---|
| `src/kdive/providers/ports/lifecycle.py` | signature, docstring | port contract |
| `src/kdive/providers/local_libvirt/lifecycle/connect.py` | stored-port branch | loopback host, range |
| `src/kdive/providers/remote_libvirt/lifecycle/connect.py`, `.../fault_inject/lifecycle/connect.py` | accept and ignore | unchanged behaviour |
| `src/kdive/jobs/payloads.py` | two optional fields | job contract |
| `src/kdive/jobs/handlers/connectivity/ssh_reachable.py`, `ssh_authorize.py` | pass port; `ready` check | worker path |
| `src/kdive/mcp/tools/lifecycle/systems/ssh_access.py` | `_recorded_endpoint` helper | server path |
| `src/kdive/services/systems/authority_owned.py` | docstring: not attach-only | authority branch |
| `src/kdive/mcp/tools/lifecycle/systems/registrar.py`, `docs/guide/reference/systems.md` | tool docs, regenerated | public contract |
| test fakes in `tests/mcp/lifecycle/`, `tests/mcp/debug/`, `tests/providers/test_composition.py`, `tests/services/external_boot/test_reverse_admission.py` | accept the keyword | fakes |

## Task 1: connector and payload contract

**Interfaces:** provides
`recorded_ssh_endpoint(self, system: SystemHandle, *, ssh_forward_port: int | None = None) -> tuple[str, int] | None`
and `ssh_forward_port: SshForwardPort | None = None` on both payload models.

**Verification**

- Stored port skips the domain read. Mode: focused-test.
  `tests/providers/local_libvirt/test_connect.py::test_recorded_endpoint_uses_a_reported_forward`;
  red: `TypeError` on the keyword. Green: `uv run python -m pytest tests/providers/local_libvirt/test_connect.py -q -k reported`.
- Out-of-range port refused before IO. Mode: focused-test.
  `test_recorded_endpoint_refuses_an_out_of_range_forward[1023|65536]`, same command; red as above.
- Payload bounds and absent-key shape. Mode: focused-test.
  `tests/jobs/test_payloads.py::test_ssh_payload_forward_port_bounds` and
  `test_ssh_payloads_omit_an_absent_forward_port`; red: extra-field validation error.
  Green: `uv run python -m pytest tests/jobs/test_payloads.py -q -k "ssh"`.

Steps:

1. Add to `tests/providers/local_libvirt/test_connect.py`, reusing `_forward_connector` and
   `_FakeSshConnect`:

```python
def test_recorded_endpoint_uses_a_reported_forward() -> None:
    connector = _forward_connector(_FakeSshConnect())
    endpoint = connector.recorded_ssh_endpoint(SystemHandle("sys-1"), ssh_forward_port=41234)
    assert endpoint == ("127.0.0.1", 41234)


@pytest.mark.parametrize("port", [1023, 65536])
def test_recorded_endpoint_refuses_an_out_of_range_forward(port: int) -> None:
    connector = _forward_connector(_FakeSshConnect())
    with pytest.raises(CategorizedError) as caught:
        connector.recorded_ssh_endpoint(SystemHandle("sys-1"), ssh_forward_port=port)
    assert caught.value.details["reason"] == "authority_ssh_forward_invalid"
```

2. Add payload tests: both models refuse `1023`, `65536`, `"2222"`, `True`; accept `1024` and
   `65535`; `dump_payload` omits the key when `None` and keeps it when set. Run, confirm red.
3. Implement. Local connector, top of `recorded_ssh_endpoint`:

```python
if ssh_forward_port is not None:
    return _LOOPBACK_HOST, _checked_reported_port(ssh_forward_port)
```

   Port, remote and fault-inject take the keyword; the port docstring says a provider that resolves
   the endpoint itself ignores it. `payloads.py` imports `SshForwardPort` from
   `kdive.providers.system_authority.protocol`.
4. Update the test fakes in the file map to accept `*, ssh_forward_port: int | None = None`.
5. Run the green commands, then commit `feat(providers): carry a reported SSH forward to the endpoint read`.

## Task 2: worker handlers

**Interfaces:** consumes Task 1. Handlers read `payload.ssh_forward_port` and pass it as the keyword.

**Verification**

- Real worker role reaches the carried port. Mode: focused-test.
  `tests/jobs/handlers/test_ssh_reachable.py::test_handler_probes_the_carried_forward_as_the_worker_role`
  and `test_ssh_authorize.py::test_handler_authorizes_over_the_carried_forward_as_the_worker_role`.
  Each opens `authority_role_dsns("kdive_worker")`, asserts
  `SELECT * FROM resolve_authority_system_control_binding(%s)` raises
  `psycopg.errors.InsufficientPrivilege` on a separate connection, then runs the handler with a
  resolver wrapping a real `LocalLibvirtConnect` whose `resolve_ssh_endpoint` raises
  `AssertionError`. Red: the endpoint comes from the raising read. Green:
  `uv run python -m pytest tests/jobs/handlers/test_ssh_reachable.py tests/jobs/handlers/test_ssh_authorize.py -q -k "carried or forward"`.
- Authorize refuses a stale carried port. Mode: focused-test.
  `test_handler_refuses_a_carried_forward_when_the_system_left_ready`; red: job proceeds; `ssh_exec`
  must stay uncalled and `details["reason"] == "system_not_ready"`. Same command.
- No port keeps today's read. Mode: focused-test. Existing
  `assert_called_once_with(SystemHandle(...))` become `..., ssh_forward_port=None)`.

Steps:

1. Write the tests above; run, confirm red.
2. `ssh_reachable.py`: pass `ssh_forward_port=payload.ssh_forward_port`.
3. `ssh_authorize.py`: same, preceded by:

```python
if payload.ssh_forward_port is not None:
    system = await SYSTEMS.get(conn, system_id)
    if system is None or system.state is not SystemState.READY:
        raise CategorizedError(
            "system is no longer ready; a carried SSH forward may belong to another System",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "system_not_ready"},
        )
```

4. Update both handler docstrings. Green, commit `fix(jobs): use the carried SSH forward in connectivity handlers`.

## Task 3: server tools and docs

**Interfaces:** consumes Tasks 1-2. Adds in `ssh_access.py`:

```python
async def _recorded_endpoint(
    conn: AsyncConnection, resolver: ProviderResolver, system: System
) -> tuple[tuple[str, int] | None, int | None]:
    """The recorded endpoint and the authority-reported port, ``None`` when none applies."""
    forward = await authority_ssh_forward_port(conn, system.id)
    binding = await resolver.binding_for_system(conn, system.id)
    handle = SystemHandle(system.domain_name or str(system.id))
    connector = binding.runtime.connector
    return connector.recorded_ssh_endpoint(handle, ssh_forward_port=forward), forward
```

**Verification**, all Mode: focused-test in `tests/mcp/lifecycle/test_systems_ssh_access.py`, with
`_mark_authority_owned(pool, sid, state=, ssh_forward_port=, provider_kind=)` extended; `activated`
also sets `first_activation_id=gen_random_uuid()`. Red: `system_domain_not_found` from a fake
connector that raises unless given the keyword. Green:
`uv run python -m pytest tests/mcp/lifecycle/test_systems_ssh_access.py -q -k authority`.

- `ssh_info`: `ready` and `activated` return the stored port; unreported returns
  `authority_ssh_forward_unreported`; `teardown-requested` returns `authority_system_not_attachable`.
- `check_ssh_reachable`: `ready` stays fenced with no provider read; `activated` stores the port in
  `job.payload`; unreported refused with no job.
- `authorize_ssh_key`: `ready` keeps the admission denial; `activated` stores the port; a restricting
  activation keeps `external_boot_restricted`; unreported refused with no job.
- Non-authority and remote-libvirt: `authority_ssh_forward_port` is `None`, payload has no key, the
  connector gets `None`.
- Tool docs. Mode: focused-test. `just docs-check` fails until regenerated.

Steps:

1. Write the tests; run, confirm red.
2. Add `_recorded_endpoint`; replace the three call sites. `check_ssh_reachable` and
   `authorize_ssh_key` pass `ssh_forward_port=forward` to the payload.
3. Reword the `authority_ssh_forward_port` docstring to cover SSH access.
4. In `registrar.py`, state for each of the three tools that an authority-owned local System uses
   its reported port and reports `authority_ssh_forward_unreported` or
   `authority_system_not_attachable`; note the preactivation fence on the two job tools. Run `just docs`.
5. Green, commit `fix(mcp): resolve authority-owned SSH endpoints from the stored port`.

## Task 4: identity and gate

- Lifecycle identity unchanged. Mode: focused-test. Run
  `uv run python -c "from kdive.processes.lifecycle.systemd.systemd_worker_contract import lifecycle_protocol_identity as i; print(i())"`
  on `origin/main` and on the branch; expected: identical strings.
- Run `just lint`, `just type`, `just test-changed`, then the final `just ci > <file> 2>&1 < /dev/null`.

Rollback: revert the branch. A job already carrying the port fails on a reverted worker; reissue.
