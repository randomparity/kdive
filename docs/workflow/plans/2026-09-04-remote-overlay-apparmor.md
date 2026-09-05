# Remote overlay AppArmor implementation plan

Issue: #2236
Spec: [remote overlay AppArmor design](../specs/2026-09-04-remote-overlay-apparmor-design.md)

The remote provider carries both libvirt-resolved paths from overlay preparation into the domain
renderer. It renders the supported two-layer file chain explicitly, letting libvirt's standard
per-domain AppArmor helper grant only the selected overlay and base.

Tech stack: Python 3.14, libvirt-python, ElementTree, pytest, and the existing Ansible/live-host
provisioning.

## Global constraints

- Work in `feat/remote-overlay-apparmor-2236`, based on `main`.
- Preserve the enabled security driver, pool/volume identity in kdive metadata, cleanup semantics, and
  the standalone-base contract.
- Grant only a libvirt-resolved backing path and encode it with ElementTree.
- Fail before domain definition on missing, malformed, or divergent reused backing metadata.
- Do not add a dependency, migration, host-wide AppArmor rule, or native ppc64le proof.
- Guardrails: `just lint`, `just type`, `just test-ansible`, focused pytest, and `just ci`.

Expected implementation size: 250–400 changed lines (M) — derived from pool refresh, remote
metadata/readback validation, XML threading, fakes, and the bounded SSH-backed live proof.

## Task 1 — Bind refreshed remote overlays to their observed base

Files: modify `src/kdive/providers/remote_libvirt/lifecycle/storage.py` and
`tests/providers/remote_libvirt/lifecycle/test_storage.py`.

Interfaces:

- `Volume.XMLDesc(flags: int = 0) -> str` supplies backing metadata for reuse validation.
- `Pool.refresh(flags: int = 0) -> int` reconstructs volume metadata from actual remote files.
- `PreparedOverlay(name: str, path: str, backing_path: str, created: bool)` is consumed by provisioning.
- `ensure_named_overlay(pool, base_volume, name) -> PreparedOverlay` returns the exact base path
  only after the base is terminal and the overlay metadata agrees.

Verification (`focused-test`): refresh must precede all volume lookups and mutations. Refreshed
nested bases must be rejected for operator-staged, newly uploaded supplied, legacy/retry supplied,
and changed-local-source scenarios. Creation/reuse must return the base path, and both must reject
missing, malformed, and mismatched backing records; created mismatch must delete the new volume.
Add those cases first and observe failures with
`uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_storage.py -q`; implement
safe defused-XML parsing and error mapping, then run the same command green.

Implementation steps:

1. Extend the fake pool/volume XML behavior and write ordering, lane, carrier, and reuse cases.
2. Add `Pool.refresh`; call it before lookup and map failure to `INFRASTRUCTURE_FAILURE` without
   exposing a path. Assert no create/define-capable mutation follows a refresh failure.
3. Extend `Volume` and `PreparedOverlay`; parse the requested base's refreshed XML, require no
   backing node, and resolve its path once.
4. Parse both a reused overlay and the volume returned by `createXML`; require their immediate
   backing path to match. Delete a just-created volume before raising on mismatch. Map malformed or
   missing metadata to `CONFIGURATION_ERROR` without leaking either path.
5. Return the resolved path on validated create and reuse; update cleanup constructors.
6. Run the focused test and `just type`, then commit.

Acceptance: no overlay can authorize a base different from the refreshed remote metadata it owns.
On validation failure no new remote mutation occurs; any pre-existing volume remains untouched.

## Task 2 — Render and thread the exact chain

Files: modify `src/kdive/providers/remote_libvirt/lifecycle/xml.py`,
`src/kdive/providers/remote_libvirt/lifecycle/provisioning.py`, and their matching provider tests.

Interfaces:

- `render_domain_xml(..., pool: str, volume: str, overlay_path: str, backing_path: str, ...) -> str`
  renders a file disk with one file backing node and an empty terminator; kdive metadata retains
  pool/volume identity.
- `_define_and_start(..., overlay: PreparedOverlay, ...)` and `_render(..., overlay, ...)` pass the
  bound name/path pair together.

Verification (`focused-test`): add XML metacharacter, exact chain, and captured pre-create domain
definition cases. Before implementation they fail because the renderer lacks `backing_path`; green
command: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_provisioning.py -q`.

Implementation steps:

1. Add failing renderer and provisioning orchestration cases.
2. Add the required keyword argument and render the explicit nested/terminal `backingStore` nodes.
3. Pass the whole `PreparedOverlay` through define/start/render so name and path cannot diverge.
4. Update existing direct renderer calls and fake expectations mechanically.
5. Run both focused provider files, `just lint`, and `just type`, then commit.

Acceptance: the definition handed to libvirt names only the prepared overlay and its bound base.
Start retry remains unchanged; teardown and external-boot admission resolve the recorded metadata
and bind the file source to the libvirt-resolved overlay path.

## Task 3 — Prove Ubuntu/AppArmor behavior and ship

Files: add `tests/live_vm/test_remote_overlay_apparmor.py` and document its opt-in control endpoint
in `tests/live_vm/__init__.py` and `docs/operating/runbooks/live-testing.md`; no host policy file is
expected.

Interfaces:

- `KDIVE_LIVE_VM_REMOTE_SSH` names the operator-provided SSH destination paired with the existing
  remote libvirt URI. Its value is never logged or persisted.
- The live fixture runs fixed argv through `ssh -- DEST sudo -n -- COMMAND...`; it accepts no shell
  fragment and redacts stdout/stderr before assertion output.
- A unique test prefix owns negative chain files, the decoy, overlay, and domain. The configured
  staged catalog base is borrowed read-only and excluded from cleanup.

Verification (`focused-test`): on the authorized clean native Ubuntu host, create test-owned chained
operator-style and pre-existing supplied-style base volumes outside libvirt metadata, then call
production `ensure_overlay` for each. Both must reject after their internal pool refresh and before
overlay/domain mutation; the supplied case replaces its local source fixture before retry. Next,
use production storage and XML functions with a standalone catalog base, start under enforcing
AppArmor, create a same-pool decoy, and verify the generated per-domain file rules contain the
selected base but not the decoy or a pool wildcard. A `finally` path removes every fixture.
Expected result: two non-skipped negative admissions, one boot/start success, and no residual test
domain, volume, or file.

Implementation steps:

1. Add the native `live_vm` test using production storage/XML functions. Gate on both the existing
   remote-live contract and `KDIVE_LIVE_VM_REMOTE_SSH`; missing opt-in skips, while any failure after
   configuration is red. Probe a unique SSH-created marker through libvirt to bind both channels to
   one host before the test.
2. Implement fixed-argv SSH helpers with noninteractive sudo and redacted errors. The cleanup
   fixture owns only unique negative files, decoy, overlay, and domain; it never touches the staged
   catalog base.
3. Run both negative remote-byte cases and assert the pool contains no derived overlay after each
   rejection.
4. Boot from the configured staged catalog base, then run exact positive and
   decoy/wildcard-negative generated-profile checks; leave the catalog base unchanged.
5. Run `just test-ansible` to prove repository host provisioning stays green.
6. Inspect and retain only redacted live success/failure facts.
7. Run `just format`, stage intended paths, run `prek run`, re-add only those paths if rewritten,
   and commit any hook-only correction separately.
8. Run focused tests, `just lint`, `just type`, and bare
   `just ci > /tmp/kdive-2236-ci.log 2>&1 < /dev/null`.

Acceptance: the clean host starts the production-shaped domain under AppArmor, every gate is green,
the staged catalog base remains byte-identical, and test-owned live artifacts are absent afterward.
