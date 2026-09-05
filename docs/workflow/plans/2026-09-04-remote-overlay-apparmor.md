# Remote overlay AppArmor implementation plan

Issue: #2236
Spec: [remote overlay AppArmor design](../specs/2026-09-04-remote-overlay-apparmor-design.md)

The remote provider will carry the libvirt-resolved base path from overlay preparation into the
existing volume-disk renderer. It will render the supported two-layer chain explicitly, letting
libvirt's standard per-domain AppArmor helper grant only the selected base.

Tech stack: Python 3.14, libvirt-python, ElementTree, pytest, and the existing Ansible/live-host
provisioning.

## Global constraints

- Work in `feat/remote-overlay-apparmor-2236`, based on `main`.
- Preserve the enabled security driver, volume disk source, pool identity, cleanup semantics, and
  the standalone-base contract.
- Grant only a libvirt-resolved backing path and encode it with ElementTree.
- Fail before domain definition on missing, malformed, or divergent reused backing metadata.
- Do not add a dependency, migration, host-wide AppArmor rule, or native ppc64le proof.
- Guardrails: `just lint`, `just type`, `just test-ansible`, focused pytest, and `just ci`.

Expected implementation size: 130–220 changed lines (M) — derived from pool refresh, remote
metadata/readback validation, the storage carrier, XML threading, fakes, and focused regressions.

## Task 1 — Bind refreshed remote overlays to their observed base

Files: modify `src/kdive/providers/remote_libvirt/lifecycle/storage.py` and
`tests/providers/remote_libvirt/lifecycle/test_storage.py`.

Interfaces:

- `Volume.XMLDesc(flags: int = 0) -> str` supplies backing metadata for reuse validation.
- `Pool.refresh(flags: int = 0) -> int` reconstructs volume metadata from actual remote files.
- `PreparedOverlay(name: str, backing_path: str, created: bool)` is consumed by provisioning.
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

- `render_domain_xml(..., pool: str, volume: str, backing_path: str, ...) -> str` renders a volume
  disk with one file backing node and an empty terminator.
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
Start retry and teardown remain unchanged.

## Task 3 — Prove Ubuntu/AppArmor behavior and ship

Files: extend the focused provider test if the live observation exposes an uncovered assertion; no
host policy file is expected.

Verification (`focused-test`): on the authorized clean native Ubuntu host, use the production
storage and XML functions to create a test-owned overlay/domain, start it with AppArmor enforcing,
create a same-pool decoy, verify the generated per-domain file rules contain the selected base but
not the decoy or a pool wildcard, and clean up in a `finally` path. Expected result: non-skipped
boot/start success and no residual test domain, overlay, or decoy.

Implementation steps:

1. Run `just test-ansible` to prove repository host provisioning stays green.
2. Run the clean-host proof and inspect only redacted success/failure facts.
3. Run `just format`, stage intended paths, run `prek run`, re-add only those paths if rewritten,
   and commit any hook-only correction separately.
4. Run focused tests, `just lint`, `just type`, and bare
   `just ci > /tmp/kdive-2236-ci.log 2>&1 < /dev/null`.

Acceptance: the clean host starts the production-shaped domain under AppArmor, every gate is green,
and test-owned live artifacts are absent afterward.
