# Authority-owned System overlay paths (#3524)

## Problem

`runs.boot` and `systems.teardown` on a System the installed authority provisioned fail with
`domain overlay ownership is absent or ambiguous`. `LocalExternalBootSessionFactory` derives the
overlay (`open`) and the teardown overlay/baseline from the worker's `ROOTFS_DIR`; the authority
provisions under `<authority_system_local_root>/rootfs/{systems,baselines}` (ADR-0623). A failed
boot also strands a `preparing` activation, because teardown fails the same way.

## Scope

- The factory takes `overlay_path_for` and `baseline_dir_for` (`SystemPath`, defaulting to the
  legacy `overlay_path`/`baseline_dir`). `open` and both teardown paths use them. They replace the
  `teardown_overlay_path`/`teardown_baseline_path` parameters (one test call site).
- `build_local_external_boot_authority` passes authority-rooted resolvers
  (`<root>/rootfs/systems/<id>.qcow2`, `<root>/rootfs/baselines/<id>-baseline`) when the host
  has an installed local System manifest; otherwise the legacy defaults stand. A host with a
  local manifest provisions every local System through the authority lane, so one root per host
  suffices; no per-System resolution.
- `_parse_owned_xml` is unchanged: exact equality against the resolver's string, one `vda` disk,
  one reference. No either-path or XML-derived fallback.
- Out of scope: worker `overlay_path` callers, vmlinux staging, reconciler dead-lettering of an
  exhausted `running` teardown job (reported, not fixed).

### Failure model

- Actors: the authority host process (fixed private roots); the worker via the authority socket.
- Invariants: only a domain whose disk is the exact host-derived path is destroyed or undefined;
  teardown deletes only the System's own overlay and baseline.
- Accepted: a pre-manifest legacy System on a manifest-bearing host is refused (fails closed);
  the manifest is read once at composition, so installing one needs a restart.
- Covered elsewhere: manifest and root validation (`validate_authority_system_installation`);
  exhausted-job dead-lettering (reported to the orchestrator).

## Success

- Authority-rooted open and teardown succeed; the legacy worker path is unchanged.
- A domain with a different disk path, or referencing the expected path twice, is refused.
- After a failed authority boot, `systems.teardown` retires the `preparing` activation and the
  domain, overlay and baseline are gone.
- Live: `runs.boot` then `systems.teardown` of an authority-owned System on a lab host.

## Validation

- Resolver used by open and teardown: focused-test, `tests/providers/local_libvirt/lifecycle/boot/test_session.py`.
- Wrong-path and doubled reference refused: focused-test, same file, via the injected resolver.
- Composition wiring (manifest present vs absent): focused-test, `tests/providers/local_libvirt/test_composition.py`.
- Failed-boot teardown retires `preparing`: focused-test, `tests/providers/system_authority/`.
- Live boot and teardown: task-test-not-applicable, needs a real libvirt host; proven in the live arm.
