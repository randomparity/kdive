# Remote deep lifecycle — implementation plan

Goal: implement [the design](../specs/2026-10-02-remote-deep-lifecycle-design.md) for #2810.

Architecture: contract ownership and remote tool scenario IDs change in `contract.py`; the live
carrier reuses #2809's `run_cell` and `deep_body` through three widened seams (SSH endpoint,
installed-kernel hook, cleanup predicate) and a new remote frame with an operator-SSH observer.

Tech stack: Python 3.14, pytest, libvirt-python, `uv`, `just`.

Expected implementation size: 550–700 changed lines (M) — contract ~20 + tests ~70, seams ~60 +
tests ~40, remote module ~230 + tests ~150, live test ~80, docs ~70. The estimate exceeds the
frozen M denominator because the remote frame and observer are new code; the band is unchanged.

## Global Constraints

- Ruff line length 100, lint `E,F,I,UP,B,SIM`; `ty` strict, whole tree (`just type`).
- No new dependency; no product source (`src/`) change; no `KDIVE_*` read under `scripts/`.
- Prose: plain; never "critical", "robust", "comprehensive", "elegant".
- Evidence artifacts never carry host names, addresses, SSH destinations, URIs or paths.
- Guardrails per task: `just lint`, `just type`, the named focused tests; `just coverage-check`
  after Tasks 1 and 5; `git fetch origin main && just records`; full `just ci` before push.

## File map

| File | Change |
|---|---|
| `scripts/coverage_campaign/contract.py` | owner routing; remote tool `scenario_id` |
| `scripts/coverage_campaign/obligations.toml` | two remote deep `[implementations]` rows |
| `tests/scripts/test_coverage_contract.py` | ownership, scenario-ID and mapping tests |
| `tests/integration/live_stack/image_smoke.py` | `Endpoint`, `GuestIdentity`; `ssh(endpoint, …)` |
| `tests/integration/live_stack/scenario.py` | endpoint-carrying SSH helpers; `cleanup_attempt`; `ACCELERATORS` |
| `tests/integration/live_stack/deep_lifecycle.py` | `installed_kernel` hook; `bound_kernel` public |
| `tests/integration/live_stack/cleanup.py` | `absent` predicate |
| `tests/integration/live_stack/remote_lifecycle.py` | new: representatives, observer, frame, bindings |
| `tests/integration/test_remote_deep_lifecycle_live.py` | new live carrier |
| `tests/integration/test_deep_lifecycle_live.py`, `test_image_smoke_live.py` | callers of the seams |
| `tests/integration/live_stack/test_{cleanup,deep_lifecycle,image_smoke,remote_lifecycle}.py` | unit tests |
| `docs/operating/runbooks/remote-live-stack.md`, `docs/development/coverage-qualification.md` | docs |

## Task 1 — contract ownership and remote tool scenario IDs

Verification:
- Ownership. Mode: focused-test — `test_lifecycle_owners_follow_the_approved_split` expects 3080
  / 2818 / 2810 owners; red before the change (remote owners are 2810). Green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.
- Scenario IDs. Mode: focused-test — new `test_remote_tool_scenarios_never_share_a_local_node`
  maps a local tool scenario and asserts the remote cells of that tool stay unmapped; red before
  the change. Same green command. Controlled fault: revert the `scenario_id` line, observe red.

Interfaces: none consumed; Task 5 relies on deep scenario IDs
`deep-lifecycle/remote-libvirt/{longterm,stable}` (unchanged).

Steps:
1. In `_tool_cells`, after `identity = (...)`, add
   `scenario_id = f"tool/{provider}/{row.tool}/{variant}" if provider == "remote-libvirt" else scenario`;
   replace the owner block with
   ```python
   owner = group.owner
   if group.owner == 3062 and arch == "ppc64le":
       owner = 2818
   elif group.owner == 3062 and provider == "remote-libvirt":
       owner = 3080
   ```
   and use `scenario_id + "/functional"` and `scenario_id + "/" + boundary` for the two
   `scenario_id` values (cell `id`s keep `identity`).
2. In `_matrix_cells`, deep owner becomes
   `2818 if arch == "ppc64le" else (2810 if provider == "remote-libvirt" else 2809)`.
3. Tests: rewrite the owner assertions of `test_lifecycle_owners_follow_the_approved_split`:
   ```python
   for operations, provider, owner in (
       (_LIFECYCLE_TOOLS, "local-libvirt", 3062),
       (_LIFECYCLE_TOOLS, "remote-libvirt", 3080),
       (deep, "local-libvirt", 2809),
       (deep, "remote-libvirt", 2810),
   ):
       assert owners(operations, provider, "x86_64") == {owner}
   for provider in ("local-libvirt", "remote-libvirt"):
       assert owners(_LIFECYCLE_TOOLS | deep, provider, "ppc64le") == {2818}
   assert len([c for c in cells if c.owner == 2810]) == 8
   assert len([c for c in cells if c.owner == 3080]) == 216
   ```
   In `test_debug_session_covers_every_advertised_transport`, expect
   `f"tool/remote-libvirt/debug.start_session/{mode}/functional"` for `remote-libvirt` and the
   unqualified form otherwise. In `test_capability_modes_and_deployed_roles_follow_operation_contract`
   (the `introspect.run` assertion) expect `tool/remote-libvirt/introspect.run/live/functional` for
   remote cells. Add:
   ```python
   def test_remote_tool_scenarios_never_share_a_local_node(inventory: Inventory) -> None:
       cells = build_contract(inventory=inventory).cells
       tools = [c for c in cells if c.id.startswith("tool/")]
       remote = {c.scenario_id for c in tools if c.provider == "remote-libvirt"}
       assert remote and all(s.startswith("tool/remote-libvirt/") for s in remote)
       assert not remote & {c.scenario_id for c in tools if c.provider != "remote-libvirt"}
       local = next(c for c in tools if c.provider == "local-libvirt" and c.owner == 3062)
       mapping = load_mapping()
       mapping.implementations[local.scenario_id] = _DEEP_NODE
       mapped = build_contract(mapping=mapping, inventory=inventory).cells
       same = [c for c in mapped if c.operation == local.operation]
       assert {c.node_id for c in same if c.provider == "remote-libvirt"} == {None}
       assert _DEEP_NODE in {c.node_id for c in same if c.provider == "local-libvirt"}
   ```
4. `rg -n "tool/" docs/development/coverage-qualification.md tests/scripts` — update any remote
   tool scenario literal found. Run the green command, `just coverage-check`, commit
   `fix(coverage): route remote lifecycle cells and qualify remote tool scenarios (#2810)`.

## Task 2 — provider-neutral seams

Verification:
- SSH endpoint. Mode: focused-test — `test_ssh_connects_to_the_endpoint_host` in
  `test_image_smoke.py` patches `subprocess.run`, calls `ssh(Endpoint("192.0.2.10", 47201), key,
  "true")`, asserts `root@192.0.2.10` and `-p 47201` in argv; red (TypeError) before. Green:
  `uv run python -m pytest tests/integration/live_stack/test_image_smoke.py -q`.
- Cleanup predicate. Mode: focused-test — `test_cleanup_uses_the_absent_predicate` passes
  `absent=lambda p: p != "/remote/kept.qcow2"` and expects `AssertionError` naming the survivor;
  red before (unexpected keyword). Green: `uv run python -m pytest tests/integration/live_stack/test_cleanup.py -q`.
- Installed-kernel proof. Mode: focused-test — `test_prove_install_owns_the_path_and_checks_the_digest`
  in `test_deep_lifecycle.py` calls `prove_install` with a matching digest and a path (owned list
  gains it, `install` proven) and with a wrong digest (AssertionError). Green:
  `uv run python -m pytest tests/integration/live_stack/test_deep_lifecycle.py -q`.
- `cleanup_attempt`, `GuestIdentity`, `ACCELERATORS` rename. Mode: task-test-not-applicable —
  signature moves with no new behavior; `just type` and the callers' existing unit tests observe them.

Interfaces (relied on by Tasks 3–4):
`Endpoint(NamedTuple: host: str, port: int)`; `GuestIdentity(Protocol)` with `distro`,
`version`, `arch` properties; `ssh(endpoint, key, command, *, deadline_s=300.0)`;
`ssh_probe(endpoint, key, command=PROBE)`; `probe_new_boot(endpoint, key, boot_id, command=PROBE)`;
`async ssh_endpoint(op, system_id) -> Endpoint`; `async authorize_ssh(...) -> tuple[Endpoint, Path]`;
`async cleanup_attempt(run, op, allocation, cleanup: Callable[[], Awaitable[dict[str, object]]] | None)`;
`InstalledKernel = Callable[[str, Endpoint, Path, str], tuple[str, str | None]]`;
`deep_body(..., entry: GuestIdentity, ..., installed_kernel: InstalledKernel)`;
`bound_kernel(root, name, arch, fixture) -> dict[str, str]`;
`release_and_verify(..., absent: Callable[[str], bool] = disk_absent)`.

Steps:
1. `image_smoke.py`: add `Endpoint` and `GuestIdentity`; `os_matches(entry: GuestIdentity, probe)`;
   `ssh` takes `endpoint` and uses `"-p", str(endpoint.port), f"root@{endpoint.host}"`.
2. `scenario.py`: rename `_ACCELERATORS` → `ACCELERATORS`; `ssh_probe`/`probe_new_boot` take
   `endpoint`; `ssh_endpoint` asserts `host_scope == "worker_loopback"`, `isinstance(host, str)`
   and `isinstance(port, int)`, returns `Endpoint(host, port)`; `authorize_ssh` returns it.
   Replace `_cleanup_attempt` with
   ```python
   async def cleanup_attempt(run, op, allocation, cleanup) -> None:
       """Best-effort release after a failed scenario; the attempt is evidence, not an assertion."""
       try:
           if cleanup is None:
               env = await scalar(op, "allocations.release", allocation_id=allocation)
               result: dict[str, object] = {"released": env.status}
           else:
               result = await cleanup()
       except Exception as exc:  # noqa: BLE001 - recorded; the scenario already failed
           result = {"error": type(exc).__name__}
       run.artifacts.append(run.writer.artifact({"cell": run.cell.id, "cleanup-attempt": result}))
   ```
   and in `on_catalog_system` call it with
   `None if system_id is None else partial(_cleanup, op, allocation, system_id, owned, in_use_before)`.
3. `cleanup.py`: add the `absent` parameter; `surviving = [p for p in disks if not absent(p)]`.
4. `deep_lifecycle.py`: rename `_bound_kernel` → `bound_kernel`; `entry: GuestIdentity`;
   `staged_kernel` → `installed_kernel: InstalledKernel`; `_install_and_boot(op, run_id)` drains
   and returns the read-back `steps` (asserting both succeeded); after the boot-identity proof:
   ```python
   installed = await asyncio.to_thread(installed_kernel, system_id, endpoint, key, manifest["release"])
   prove_install(run, steps, installed, owned, boot_kernel_sha256(tree, manifest["arch"]))


   def prove_install(run, steps, installed, owned, expected) -> None:
       digest, path = installed
       if path is not None:
           owned.append(path)
       assert digest == expected, "the installed kernel is not the uploaded boot member"
       run.observed["kernel_sha256"] = digest
       run.prove("install", {"steps": dict(steps), "kernel_sha256": digest})
   ```
5. `test_deep_lifecycle_live.py`: `_domain_kernel(system_id, _endpoint, _key, _release)` returns
   `(file_sha256(kernel), kernel)`; pass `installed_kernel=_domain_kernel`. `test_image_smoke_live.py`:
   rename `port` → `endpoint` in its four SSH calls.
6. Add the three focused tests; run their green commands, `just lint`, `just type`; commit
   `refactor(live-stack): carry the SSH host and installed-kernel hook through the shared frame`.

## Task 3 — remote frame, observer and bindings

Verification (all focused-test in new `tests/integration/live_stack/test_remote_lifecycle.py`,
green `uv run python -m pytest tests/integration/live_stack/test_remote_lifecycle.py -q`, red =
ImportError before the module exists):
- Representatives: every remote deep cell family is in exactly one of `REMOTE_REPRESENTATIVES`
  / `REMOTE_BLOCKED`; each representative's name/distro/version is a `kdive_image_catalog` row of
  `deploy/ansible/inventory/group_vars/all.yml` (read with `yaml.safe_load`) and its `distro`
  maps to the cell family under the local catalog (`image_family`).
- Destination: `dave@lab-a.example` accepted; `-oProxyCommand=x`, `a b` and the empty string rejected (`ValueError`).
- `host_probe` parses a faked `subprocess.run` stdout into `host_os`, `host_arch`, `virt`;
  non-zero exit → `AssertionError`.
- `volume_absent`: refreshes every active pool; `VIR_ERR_NO_STORAGE_VOL` → True; a found volume →
  False; any other libvirt error propagates.
- `guest_boot_kernel` sends `sha256sum -- /boot/vmlinuz-6.18.54` (via a patched `ssh`) and
  returns `(digest, None)`; a release `6.1'x` is sent as `shlex.quote("/boot/vmlinuz-6.1'x")`.
- Frame identity: with a faked probe (`rocky:10`, `x86_64`), the `run.observed` host fields the
  frame's `observe_host(run, host)` sets make `run.context(identity)` report the probe's host,
  not `identity`'s, and equal what `bindings` writes for the same probe; a probe arch other than
  the cell's raises `ScenarioStop(BLOCKED)`; the `provider_host` artifact carries `os`, `arch`
  and `virtualization`.
- `remote_kdive_domains` lists only `kdive-`-prefixed defined domain names of a faked connection;
  `on_remote_system`'s post-cleanup check fails when a `kdive-*` domain not present before the
  allocation remains.
- `remote_profile` parses through `ProvisioningProfile.parse` with the given volume.
- `bindings` binds exactly `remote_cells()` (the 8 owner-2810 cells); representatives get guest
  identity, `kvm`, the faked volume digest and fixture kernel inputs; blocked families get null
  guest fields; no ppc64le cell appears.

Interfaces: consumes Task 2; Task 4 relies on `REMOTE_BLOCKED: dict[str, str]`,
`REMOTE_REPRESENTATIVES: dict[str, RemoteImage]`, `remote_cells() -> list[Cell]` (owner 2810),
`guest_boot_kernel: InstalledKernel`, `async on_remote_system(run, base_url, issuer, db_url, *, project, family, body: CatalogBody)`.

Steps:
1. Create `remote_lifecycle.py` with the module docstring of the design's item 3 and:
   `HOST_SSH_ENV = "REMOTE_PROVIDER_SSH"`; `RemoteImage` frozen dataclass; the two maps;
   `remote_cells()` (contract `deep-lifecycle` cells with `owner == 2810`);
   `HOST_PROBE = 'cat /etc/os-release; printf "machine=%s\\nvirt=%s\\n" "$(uname -m)" "$(systemd-detect-virt || true)"'`;
   `RemoteHost(dest, pool, host_os, host_arch, virt)` frozen dataclass;
   `destination()` (env → `None` | validated str, `ValueError` otherwise);
   `host_probe(dest)` (`ssh -o BatchMode=yes -o ConnectTimeout=10 dest HOST_PROBE`, timeout 60,
   `os_identity(stdout)`, `key_values` for `machine`/`virt`);
   `observer(dest)` (`libvirt.open(f"qemu+ssh://{dest}/system?no_tty=1")`);
   `volume_absent(conn, path)`; `@cache volume_sha256(dest, pool, volume)` (`vol.download` into
   `conn.newStream(0)`, `stream.recvAll(_feed, digest)` with
   `def _feed(_stream, data: bytes, digest) -> int: digest.update(data); return len(data)`,
   `stream.finish()`, connection closed in `finally`);
   `staged_volume(name)` (the `remote-libvirt` `[[image]]` with a `StagedSource`);
   `remote_host()` (raises `ScenarioStop(Outcome.BLOCKED, …)` for an unset/invalid destination,
   instance count ≠ 1, or a failed probe; pool from `remote_config_for_resource(name).storage_pool`);
   `observe_host(run, host)` (`ScenarioStop(BLOCKED)` unless `host.host_arch == run.cell.guest_arch`;
   then `run.observed |= {"host_os": …, "host_arch": …}` and the artifact
   `{"cell": …, "provider_host": {"os": …, "arch": …, "virtualization": host.virt}}`);
   `remote_kdive_domains(conn) -> set[str]` (`{d.name() for d in conn.listAllDomains(0) if
   d.name().startswith("kdive-")}`);
   `remote_profile(arch, volume)` (`disk-image`, `vcpu` 2, `memory_mb` 2048,
   `REMOTE_ALLOCATION_DISK_GB`, `kernel_source_ref` `"remote-deep-lifecycle-unread"`,
   `{"remote-libvirt": {"base_image_volume": volume}}`);
   `guest_boot_kernel(_system_id, endpoint, key, release)`;
   `on_remote_system` exactly as design item 3 (`observe_host` first; `remote_kdive_domains`
   read before `allocations.request`; after `release_and_verify` assert the set is unchanged and
   add `"remote_domains": "unchanged"` to the cleanup observation; domain XML read through `observer`; cleanup
   `release_and_verify(..., connect=partial(observer, host.dest), absent=partial(volume_absent, conn))`
   with `conn = observer(host.dest)` closed afterwards; `cleanup_attempt` on failure);
   `bindings(candidate, *, root, matrix, host, digest=volume_sha256, fixture=load_fixture,
   staged=staged_volume)`; `main` (`bindings --candidate --out`; a `ScenarioStop` prints its
   reason and returns 2).
2. Write the tests above; green; `just lint`; `just type`; commit
   `test(live-stack): add the remote deep-lifecycle frame and observer (#2810)`.

## Task 4 — live carrier and mapping

Verification:
- Mapping. Mode: focused-test — `test_pending_cells_have_owned_assertions_but_no_invented_nodes`
  asserts remote deep cells carry `tests/integration/test_remote_deep_lifecycle_live.py::test_remote_deep_lifecycle`;
  red before the `obligations.toml` rows. Green: `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`
  and `just coverage-check`.
- Live carrier body. Mode: task-test-not-applicable — needs a remote libvirt host, stack and
  fixtures; proven by the recorded live run (Task 6), not by a unit test.

Steps:
1. `test_remote_deep_lifecycle_live.py` (`pytestmark = pytest.mark.live_stack`, project
   `"remote-deep-lifecycle"`): `_deep(run, base_url, issuer, db_url, *, tmp)` stops `BLOCKED`
   with `REMOTE_BLOCKED[family]` for a blocked family, then the fixture checks of
   `test_deep_lifecycle_live._deep`, then `on_remote_system(..., family=family, body=body)` where
   `body` calls `deep_body(..., entry=REMOTE_REPRESENTATIVES[family], installed_kernel=guest_boot_kernel)`,
   then re-verifies the fixture. `test_remote_deep_lifecycle(cell, tmp_path)` parametrized over
   `remote_cells()` (ids `f"{c.family}-{baseline(c)}"`) runs
   `run_cell(cell, partial(_deep, tmp=tmp_path))`.
2. `obligations.toml` `[implementations]`: the two `deep-lifecycle/remote-libvirt/*` rows; update
   the mapping test; green; commit `test(live-stack): drive the remote deep lifecycle (#2810)`.

## Task 5 — docs

Verification: Mode: task-test-not-applicable — runbook prose; no executable consumer (doc
checks run in `just ci`).

Steps: add "## 7. Remote deep lifecycle (#2810)" to `remote-live-stack.md`: topology (control
plane runs the stack and pytest; a separate x86_64 provider host prepared with the
`libvirt_tls`/`libvirt_pool_net` roles and `image.yml` with `host_images` set to the two
representatives); observer access (`REMOTE_PROVIDER_SSH`, libvirt group, known host key);
`ssh_addr`/`ssh_range` required, with a source-restricted firewalld rich rule opening that range
to the control plane (as `gdbstub_acl` does for the gdbstub range); fixtures (`KDIVE_FIXTURE_ROOT`, as the live-testing runbook);
`python -m tests.integration.live_stack.remote_lifecycle bindings`, the pytest node, `evidence
assemble`, `qualify`; blocked families and owners; manual release after a killed run. In
`coverage-qualification.md` say the remote deep lifecycle is bound and remote tool scenario IDs
are provider-qualified. Commit `docs: run the remote deep lifecycle (#2810)`.

## Task 6 — live run (operator lane)

On lab test hosts: control plane on a Fedora host at the candidate SHA (stack via the documented
installer path), a separate Rocky provider host prepared by the repo's Ansible. Build both
fixtures, stage both representatives, write bindings, run the 8 x86_64 cells, assemble, qualify.
Record per-cell outcomes (sanitized) in the PR; file a linked issue for each failure; restore the
lab hosts afterwards.
