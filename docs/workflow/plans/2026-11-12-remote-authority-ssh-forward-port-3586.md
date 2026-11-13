# Remote authority reports ssh_forward_port — plan (#3586)

Goal: the remote-libvirt authority reports the System's SSH port in its provision facts and
refuses a manifest `ssh_port_min` below 1024 at startup and in the Ansible preflight.

Architecture: three extensions of existing owners — the remote facts builder, the remote
manifest entry's `__post_init__` (which the manifest loader already runs), and the stdlib
preflight script. Spec: `docs/workflow/specs/2026-11-12-remote-authority-ssh-forward-port-3586-design.md`.

Tech stack: Python 3, pydantic, pytest (anyio), uv, just.

Expected implementation size: 60–110 changed lines (M) — three small source edits, one comment
edit, and four focused tests across three existing test files.

## Global Constraints

- Floor value is `SSH_FORWARD_PORT_MIN` (1024) from `kdive.providers.system_authority.protocol`
  in `src/`; the preflight script is stdlib-only and uses the literal `1024`.
- No schema, receipt, payload, or Operation change; lifecycle identity must not move.
- Do not edit files owned by #3585 (`services/systems/authority_owned.py`,
  `remote_libvirt/lifecycle/connect.py`, `ports/lifecycle.py`, `jobs/payloads.py`,
  `jobs/handlers/connectivity/`, `mcp/tools/`). Keep `remote_libvirt/system_authority.py` edits
  minimal (#3601 edits near line 630).
- Guardrails: `just lint`, `just type`, `just test-changed`; final `just ci`.

## File map

| File | Change |
|---|---|
| `src/kdive/providers/remote_libvirt/system_authority.py` | facts carry the port; entry floor |
| `src/kdive/providers/system_authority/protocol.py` | comment wording only |
| `deploy/ansible/scripts/validate_authority_system_provisioning.py` | preflight floor |
| `tests/providers/remote_libvirt/test_system_authority.py` | facts port tests |
| `tests/providers/system_authority/test_manifest.py` | runtime floor test |
| `tests/deploy/test_provider_authority_local_provisioning.py` | preflight floor test |

## Task 1: remote facts carry ssh_forward_port

Verification:
- facts port. Mode: focused-test. In `test_provider_implements_fixed_port_and_persists_intent_before_mutation`
  add `assert facts.ssh_forward_port == 47100`; in `test_observe_complete_provision_changes_no_private_or_libvirt_state`
  add `assert facts.ssh_forward_port == 47100`; in `test_absent_observation_is_read_only` add
  `assert facts.ssh_forward_port is None`; in `test_interrupted_definition_reuses_overlay_ports_and_retained_deadline`,
  right after `assert first["phase"] == "overlay-ready"`, add
  `partial = await provider.observe_system_provision(_request(), _context(), _snapshot())` and
  `assert not partial.complete and partial.ssh_forward_port is None` (a present but incomplete
  intent reports no port). Red: the two complete-path asserts fail with `None == 47100`.
  Green: `uv run pytest tests/providers/remote_libvirt/test_system_authority.py -q`.

Steps:
1. Add the asserts above; run the command; expect 2 failures.
2. In `_observe_provision_locked`, add to the returned `AuthoritySystemProvisionFacts(...)`:
   `ssh_forward_port=intent.ssh_port if complete else None,`
3. Rerun; expect all pass. Commit `feat(authority): report ssh_forward_port from the remote intent`.

## Task 2: manifest floor in the authority

Verification:
- runtime floor. Mode: focused-test. Add to `tests/providers/system_authority/test_manifest.py`:

```python
@pytest.mark.parametrize(("ssh_port_min", "accepted"), ((1023, False), (1024, True)))
def test_remote_entry_refuses_ssh_range_below_receipt_floor(
    tmp_path: Path, ssh_port_min: int, accepted: bool
) -> None:
    path = tmp_path / "manifest.json"
    document = _remote_document()
    # Keep the ssh range clear of the fixture's gdb range (31000-31002) on the same address.
    document["entries"][0].update(ssh_port_min=ssh_port_min, ssh_port_max=ssh_port_min + 2)
    _write_manifest(path, document)

    if accepted:
        result = load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())
        assert isinstance(result, RemoteAuthoritySystemManifestV1)
        return
    with pytest.raises(ValidationError, match="starts below 1024"):
        load_authority_system_manifest(path, owner_uid=os.getuid(), owner_gid=os.getgid())
```

  Red: the 1023 case loads (no floor yet). Green:
  `uv run pytest tests/providers/system_authority/test_manifest.py -q`.

Steps:
1. Add the test; run; expect the 1023 case to fail.
2. In `remote_libvirt/system_authority.py`, add `SSH_FORWARD_PORT_MIN` to the imports from
   `kdive.providers.system_authority.protocol` (it is not re-exported from the package
   `__init__`), and in `RemoteAuthoritySystemManifestEntry.__post_init__`, after the range loop:

```python
        if self.ssh_port_min < SSH_FORWARD_PORT_MIN:
            raise ValueError(f"authority ssh port range starts below {SSH_FORWARD_PORT_MIN}")
```

3. Rerun; expect pass. Commit `feat(authority): refuse a remote ssh port range below 1024`.

## Task 3: preflight floor

Verification:
- preflight floor. Mode: focused-test. Add to `tests/deploy/test_provider_authority_local_provisioning.py`:

```python
@pytest.mark.parametrize(("ssh_port_min", "accepted"), ((1023, False), (1024, True)))
def test_system_provisioning_validator_refuses_remote_ssh_range_below_1024(
    tmp_path: Path, ssh_port_min: int, accepted: bool
) -> None:
    # Keep the ssh range clear of the fixture's gdb range (31000-31002) on the same address.
    value = _remote_validator_input(
        tmp_path, ssh_port_min=ssh_port_min, ssh_port_max=ssh_port_min + 2
    )

    assert (_run_system_provisioning_validator(value).returncode == 0) is accepted
```

  Red: 1023 exits 0. Green:
  `uv run pytest tests/deploy/test_provider_authority_local_provisioning.py -q`.

Steps:
1. Add the test; run; expect the 1023 case to fail.
2. In `validate_authority_system_provisioning.py`, after the gdb/ssh range loop in the remote
   entry branch, add:

```python
            if entry["ssh_port_min"] < 1024:
                _fail()
```

3. Rerun; expect pass. Commit `feat(deploy): refuse a remote ssh port range below 1024 in preflight`.

## Task 4: wording, identity, gate

Verification:
- comment wording. Mode: task-test-not-applicable — prose comments in `protocol.py` with no
  executable consumer.
- lifecycle identity. Mode: focused-test. Run
  `uv run python -c "from kdive.processes.lifecycle.systemd.systemd_worker_contract import lifecycle_protocol_identity as i; print(i())"`
  in the main checkout at `origin/main` and in this worktree; expected: identical strings.

Steps:
1. In `protocol.py`, change the `SSH_FORWARD_PORT_MIN` comment to
   `# The unprivileged range an authority draws its guest SSH forward port from (ADR-0811, ADR-0815).`
   and the receipt field comment's first sentence to
   `# Additive (ADR-0811, ADR-0815): the port of the System's guest SSH forward, on loopback for a
   local authority and on the instance's ssh_addr for a remote one.` keeping the second sentence.
2. Run the identity command on both trees; compare.
3. `just lint`, `just type`, `just test-changed`; commit `docs(authority): drop the loopback-only wording on the ssh forward port`.
4. Final gate: `just ci > <file> 2>&1 < /dev/null`.

Rollback: revert the branch. A remote manifest refused by the floor starts again after revert.
