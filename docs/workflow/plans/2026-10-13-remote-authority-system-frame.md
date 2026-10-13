# Remote authority System frame (#3209) — plan

Goal: a `remote-libvirt` cell runs inside `on_authority_system` on the installed remote authority
lane instead of stopping `blocked`.
Architecture: a fixed root program over the existing bounded SSH seam observes the remote
authority's private daemon and pool (`installed_remote_authority_support.py`); a remote branch in
`authority_lane.py` provisions through public HTTP on the authority-routed resource, runs the body,
and cleans up in the spec's order, reusing the local branch's drain, close, release and grouping.
Tech stack: Python 3.14, pytest, psycopg, libvirt-python (remote, authority venv).
Spec: `docs/workflow/specs/2026-10-13-authority-system-frame-design.md`, section
"Addendum: remote authority branch (#3209)".

Expected implementation size: 1050–1150 changed lines (M) — corrected after the build from the
actual diff (1101 lines): support observer and gates ~125, frame branch and shared-helper
extraction ~350, unit tests ~550 (the remote recorder, parametrized stops and cleanup failures, the
program run against a fake libvirt, the catalog query against the migrated database), live arm and
runbook ~80. The first estimate (330–430) under-counted the tests and the extraction; the frozen
scope and design are unchanged.

## Global Constraints

- No production code, API, schema, provider or scheduler change; no new dependency.
- `tool_cells.py`, `Lane`, `LaneFrame`, `lane_for` unchanged. `RemoteAuthorityProofConfig` fields
  unchanged.
- Evidence and messages never carry the SSH target, pool name, host paths beyond the fixed roots,
  or exception messages from cleanup (type names only).
- Guardrails: `just test-verbose <paths>`, `just lint`, `just type`, `just docs-links`,
  `just docs-paths`. Commit after each task; prove each new test bites (commit, inject a fault,
  see red, `git checkout -- <file>`).

## File map

| File | Change |
|---|---|
| `tests/live_vm/installed_remote_authority_support.py` | `_ssh` gains `limit`/`timeout` keywords; adds `_INVENTORY`, `RemoteDomain`, `RemoteAuthorityInventory`, `remote_authority_inventory`, `require_installed_authority` |
| `tests/integration/live_stack/authority_lane.py` | remote branch; extracts `_running_workers`, `_require_systems_toml`, `_allocate_and_run`, `_release`, `_require_unchanged` from the local branch; removes `REMOTE_BLOCKED` |
| `tests/live_vm/test_installed_remote_authority_support.py` | observer and gate tests |
| `tests/integration/live_stack/test_authority_lane.py` | remote frame tests; drops `test_remote_cell_is_blocked` |
| `tests/integration/test_authority_lane_frame_live.py` | parametrized local/remote arms |
| `docs/operating/runbooks/live-testing.md` | remote frame usage under "Authority-owned System frame" |

## Task 1 — remote observer and gates

Files: `tests/live_vm/installed_remote_authority_support.py`,
`tests/live_vm/test_installed_remote_authority_support.py`.

Interfaces (later tasks rely on these exact signatures):

```python
@dataclass(frozen=True)
class RemoteDomain:
    active: bool
    kind: str


@dataclass(frozen=True)
class RemoteAuthorityInventory:
    arch: str
    domains: list[str]
    volumes: list[str]
    system: RemoteDomain | None


def remote_authority_inventory(
    config: RemoteAuthorityProofConfig, system_id: str | None = None
) -> RemoteAuthorityInventory: ...
def require_installed_authority(config: RemoteAuthorityProofConfig) -> None: ...
```

Verification:
- Mode: focused-test — command shape: the inventory runs `ssh … -- <target> 'sudo -n
  /opt/kdive-provider-authority/.venv/bin/python -I -c <program> [<uuid>]'` with stdout limit
  1 MiB, timeout 120; a non-UUID argument raises before SSH. Red: function absent (ImportError).
- Mode: focused-test — parsing: missing/extra keys, non-string names, non-bool `active`, invalid
  JSON each raise `AssertionError("remote authority inventory is malformed")`.
- Mode: focused-test — the program: executed with a fake `libvirt` module and a fake `open`, it
  uses the env file's pool, defaults to `default`, rejects `bad/pool`, and reports the System's
  facts. Red: wrong pool name in the fake's lookup.
- Mode: focused-test — gates: an inactive unit (`systemctl is-active` exits non-zero, so the
  seam raises `RuntimeError`) and a revision mismatch (`AssertionError`) both refuse.

Steps:

1. Add to `_ssh` keyword-only `limit: int = _MAX_CONTROL_BYTES, timeout: float = 15` and pass them
   as `stdout_limit=limit, timeout=timeout`; existing callers keep defaults.
2. Add `_AUTHORITY_VENV_PYTHON = "/opt/kdive-provider-authority/.venv/bin/python"`,
   `_REVISION = "/opt/kdive-provider-authority/revision"`, `_MAX_INVENTORY_BYTES = 1_048_576`, and
   `_INVENTORY`: the merged local program (`authority_lane.py` `_INVENTORY`, same imports, UUID
   argument, socket and System facts) with its private-root listing replaced by the pool read
   below, and the output `{"arch": os.uname().machine, "domains": ..., "volumes": ...,
   "system": ...}`:

```python
pool_name = "default"
with open("/etc/kdive/provider-authority.env", encoding="utf-8") as environment:
    for line in environment:
        key, _, value = line.rstrip("\n").partition("=")
        if key == "KDIVE_REMOTE_LIBVIRT_STORAGE_POOL":
            pool_name = value
if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", pool_name) is None:
    raise SystemExit("installed authority storage pool name is invalid")
# ... inside the connection's try block, after listing the domains:
pool = conn.storagePoolLookupByName(pool_name)
pool.refresh(0)
volumes = sorted(pool.listVolumes())
```

3. Add the dataclasses above and:

```python
def _names(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise AssertionError("remote authority inventory is malformed")
    return list(value)


def _remote_domain(value: object) -> RemoteDomain | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"active", "type"}:
        raise AssertionError("remote authority inventory is malformed")
    active, kind = value["active"], value["type"]
    if not isinstance(active, bool) or not isinstance(kind, str):
        raise AssertionError("remote authority inventory is malformed")
    return RemoteDomain(active, kind)


def remote_authority_inventory(config, system_id=None):
    """The remote authority daemon's domains, its private pool's volumes and one System's domain."""
    command = ("sudo", "-n", _AUTHORITY_VENV_PYTHON, "-I", "-c", _INVENTORY)
    if system_id is not None:
        command += (str(UUID(system_id)),)
    stdout = _ssh(config, command, limit=_MAX_INVENTORY_BYTES, timeout=120)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise AssertionError("remote authority inventory is malformed") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"arch", "domains", "volumes", "system"}
        or not isinstance(data["arch"], str)
    ):
        raise AssertionError("remote authority inventory is malformed")
    return RemoteAuthorityInventory(
        data["arch"],
        _names(data["domains"]),
        _names(data["volumes"]),
        _remote_domain(data["system"]),
    )


def require_installed_authority(config):
    """Refuse unless the remote authority unit is active at the configured revision."""
    if _ssh(config, ("systemctl", "is-active", config.authority_service)) != b"active\n":
        raise AssertionError("the remote authority service is not active")
    revision = _ssh(config, ("sudo", "-n", "cat", _REVISION)).strip()
    if revision != config.installed_revision.encode():
        raise AssertionError("the installed remote authority revision does not match the config")
```

4. Tests: fake `carrier._run_bounded_command` as the existing barrier tests do; for the program,
   `exec(compile(carrier._INVENTORY, "inventory", "exec"), {"__name__": "inventory", "open":
   fake_open})` with `monkeypatch.setitem(sys.modules, "libvirt", fake)` and
   `monkeypatch.setattr(sys, "argv", ["-c", uuid])`, reading stdout through `capsys`.
5. `just test-verbose tests/live_vm/test_installed_remote_authority_support.py` → all pass. Commit.

## Task 2 — remote branch of the frame

Files: `tests/integration/live_stack/authority_lane.py`,
`tests/integration/live_stack/test_authority_lane.py`.

Interfaces: consumes Task 1's `remote_authority_inventory`, `require_installed_authority`,
`RemoteAuthorityProofConfig`, `load_config`; existing `remote_config_for_resource(name) ->
RemoteLibvirtConfig` (`.authority: RemoteAuthorityBinding | None`, `.authority_instance`),
`REMOTE_ALLOCATION_DISK_GB` (spine), `require_deployed_revision(config, base_url, running)`,
`require_root_provenance`, `drain_system_jobs`, `_close_investigations`, `_cleanup_after_failure`.
Provides `REMOTE_CONFIG_ENV`, `select_remote_lane() -> RemoteAuthorityProofConfig`,
`require_remote_lane(config, base_url) -> None`, `remote_root(db_url, resource, arch) ->
tuple[str, str, str]` (resource id, image name, digest), `remote_authority_profile(arch, image)`.
`AuthoritySystem.config` becomes `NativeAuthorityConfig | RemoteAuthorityProofConfig`.

Verification:
- Mode: focused-test — stops: unset config, undeclared or unrouted resource, zero or two root
  images, foreign host arch each raise `ScenarioStop(BLOCKED)` with no `allocations.request`.
  Red today: the remote cell stops with "#3209".
- Mode: focused-test — success order: `require_remote_lane`, evidence access, `remote_root`,
  inventory, `allocations.request`, `systems.provision`, root provenance, inventory(System), body,
  drain, `systems.teardown`, inventory(System), `allocations.release`, inventory; `effect` and
  `cleanup` proven.
- Mode: focused-test — cleanup failures: a remaining domain or a volume naming the System fails
  before release; inventory drift fails; a body failure plus cleanup failure groups both.
- Mode: focused-test — prefix: the body's `config.ownership_prefix` matches the prefix pattern,
  differs from the file's, and differs across two invocations.
- Mode: focused-test — `remote_root` against the migrated test database: a staged row without a
  digest is not selected; one verified row is; two are a `blocked` stop.
- Mode: focused-test — local branch unchanged: the existing local tests stay green.

Steps:

1. Extract from the local branch: `_running_workers()` (the `systemctl list-units` call in
   `require_lane`), `_require_systems_toml()` (the `KDIVE_SYSTEMS_TOML` check in `select_lane`),
   `_release(frame)` (the no-System release block of `_cleanup`), `_require_unchanged(before,
   after)` (the inventory comparison), and `_allocate_and_run(run, op, frame args, resource,
   disk_gb, create)` (capacity, allocation, `_Frame`, try/except cleanup, the two proofs). The local
   branch calls them unchanged in behavior.
2. Add `remote: RemoteAuthorityProofConfig | None = None` to `_Frame`; `_cleanup` dispatches to
   `_remote_cleanup(frame, frame.remote)` when set.
3. `on_authority_system` starts with `if run.cell.provider == "remote-libvirt": return await
   _on_remote_authority_system(...)`; delete `REMOTE_BLOCKED` and its select check.
4. `select_remote_lane`: load config (unset → blocked naming `REMOTE_CONFIG_ENV`);
   `_require_systems_toml()`; `remote_config_for_resource(resource_name)` (a `CategorizedError`
   or a binding whose instance differs → blocked); mint
   `f"kdive-2151-{revision[:12]}-{secrets.token_hex(4)}"` and return
   `RemoteAuthorityProofConfig.model_validate(config.model_dump() | {"ownership_prefix": prefix})`.
5. `remote_root`: read-only connection; resource id from `resources` (kind `remote-libvirt`, name);
   images from `image_catalog` where provider `remote-libvirt`, the arch, `state = 'registered'`,
   `visibility = 'public'`, `digest IS NOT NULL`, `provenance->'root_spec' IS NOT NULL`; anything
   but one row or a missing resource → blocked naming the precondition and the count.
6. `_on_remote_authority_system` (spec lifecycle 1–3) and `_remote_cleanup` (step 4):

```python
async def _on_remote_authority_system(run, base_url, issuer, db_url, *, project, body):
    config = select_remote_lane()
    arch = run.cell.guest_arch
    await asyncio.to_thread(require_remote_lane, config, base_url)
    await require_evidence_read_access(db_url)
    resource_id, image, digest = await remote_root(db_url, config.resource_name, arch)
    observed = await asyncio.to_thread(remote_authority_inventory, config)
    if observed.arch != arch:
        raise ScenarioStop(
            Outcome.BLOCKED,
            f"the remote authority host is {observed.arch}; {run.cell.id} needs {arch}",
        )
    token = mint_role_token(issuer, project=project, agent_session=f"{project}-sess", role="admin")
    async with LiveStackClient.over_http(base_url, token) as op:
        await seed_metering(db_url, project)
        run.observed |= {"image_sha256": digest.removeprefix("sha256:"), "guest_arch": arch}

        async def create(frame: _Frame) -> dict[str, object]:
            profile = remote_authority_profile(arch, image)
            return await _create_remote_and_run(run, frame, config, profile, digest, body)

        await _allocate_and_run(
            run,
            op,
            db_url,
            project,
            _remote_state(observed),
            resource={"mode": "id", "resource_id": resource_id},
            disk_gb=REMOTE_ALLOCATION_DISK_GB,
            create=create,
            remote=config,
        )


async def _create_remote_and_run(run, frame, config, profile, digest, body):
    op = frame.op
    frame.ledger = ResourceLedger(config.ownership_prefix)
    provisioned = ok(
        await scalar(op, "systems.provision", allocation_id=frame.allocation, profile=profile),
        "provision",
    )
    system_id = frame.system_id = data_str(provisioned, "system_id")
    await await_system_state(op, "provision", system_id, "ready")
    await require_root_provenance(frame.db_url, system_id, digest, str(profile["arch"]))
    facts = (await asyncio.to_thread(remote_authority_inventory, config, system_id)).system
    assert facts is not None and facts.active, (
        "the System's domain is not running on the remote authority daemon"
    )
    run.observed["accelerator"] = ACCELERATORS.get(facts.kind, "none")
    return await body(
        AuthoritySystem(op, frame.project, frame.allocation, system_id, config, frame.ledger)
    )


async def _remote_cleanup(frame, config):
    op, owned, drained = frame.op, [frame.allocation], []
    if frame.system_id is not None:
        drained = await drain_system_jobs(op, frame.db_url, frame.system_id)
        if frame.ledger is not None:
            await _close_investigations(op, frame.ledger)
        ok(await scalar(op, "systems.teardown", system_id=frame.system_id), "teardown")
        await await_system_state(op, "teardown", frame.system_id, "torn_down")
        left = await asyncio.to_thread(remote_authority_inventory, config, frame.system_id)
        assert left.system is None, "the System's domain remains on the remote authority daemon"
        stranded = [volume for volume in left.volumes if frame.system_id in volume]
        assert not stranded, f"the System's volumes remain in the authority pool: {stranded}"
        owned += [frame.system_id, domain_name_for(UUID(frame.system_id))]
    released = await _release(frame)
    after = _remote_state(await asyncio.to_thread(remote_authority_inventory, config))
    _require_unchanged(frame.before, after)
    return {
        **released,
        "jobs_drained": sorted(drained),
        "owned": owned,
        "inventory_sha256": _digest(after),
    }
```

`_remote_state(inventory)` returns `{"remote_authority_domains": inventory.domains,
"remote_authority_volumes": inventory.volumes}`. `remote_authority_profile` returns the
`remote_profile` shape of `remote_lifecycle.py` with `"base_image_source": {"kind": "catalog",
"provider": "remote-libvirt", "name": image}` in place of `base_image_volume`.
7. Tests use a `_fake_remote_lane` recorder like `_fake_lane`, with a real mode-0600 proof config.
8. `just test-verbose tests/integration/live_stack/test_authority_lane.py` → pass; `just lint`;
   `just type`. Commit.

## Task 3 — live arm and runbook

Files: `tests/integration/test_authority_lane_frame_live.py`,
`docs/operating/runbooks/live-testing.md`.

Verification:
- Mode: task-test-not-applicable — the live arm only runs on the installed lanes; offline it
  skips by design, so no offline observation can fail meaningfully. Its live run is the proof.
- Mode: task-test-not-applicable — runbook prose; `just docs-links` and `just docs-paths` check
  its references.

Steps:

1. Parametrize the two live tests over `("local-libvirt", CONFIG_ENV)` and
   `("remote-libvirt", REMOTE_CONFIG_ENV)`; skip an arm whose variable is unset. The remote arm's
   `Cell` uses `provider="remote-libvirt"` and `host_arch = guest_arch = "x86_64"` (the remote
   lane's family, `REMOTE_LANE_FAMILIES`), never the control plane's `platform.machine()`.
2. Runbook: replace "The remote authority frame is #3209 …" with the remote branch's inputs
   (proof config, minted prefix, authority-routed resource, verified remote root image, one
   scenario at a time on the remote authority) and its manual cleanup after a killed run.
3. `just docs-links`, `just docs-paths`, `just lint`. Commit.

## Live proof (held)

After #3215 merges and the root approves a private plan naming its SHA: run both arms of the live
test on the installed lanes at the candidate. Relied-on #3215 behavior: a lane bring-up whose
`KDIVE_HARNESS_DATABASE_URL` is valid with an `ADDR:PORT` Postgres port; the worker's local
authority route is not used by the remote branch.

## Deferrals

None.
