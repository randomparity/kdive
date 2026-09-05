# Boot-artifact name ownership implementation plan

Goal: replace discarded volume metadata with the versioned, bounded name contract in ADR-0599.

Architecture: a pure name codec is shared by the remote-libvirt producer and reaper. The producer
computes identity before creating storage; the reaper parses names and verifies complete bytes
before deletion. Python 3.14, `libvirt-python`, stdlib hashing/XML, and pytest remain the stack.

Expected implementation size: 220–360 changed lines (M) — derived from one codec, two consumers,
shared-double operations, and focused/live tests.

## Global constraints

- Grammar is exact ADR-0599 version 1, ASCII, and at most 203 bytes under the proven 255-byte
  dir-pool limit.
- Unknown, legacy, malformed, mismatched, unreadable, and live-owner objects are never deleted.
- No metadata fallback, schema/dependency change, remote-module change, or ppc64le proof.
- Run `just lint`, `just type`, focused pytest, then `just ci > <file> 2>&1 < /dev/null` before push.
- Branch: `feat/boot-artifact-name-ownership-2240`; base: `main` at `6ff6c7077`.

## Task 1 — Add the closed name codec

Files: create
`src/kdive/providers/remote_libvirt/lifecycle/rootfs/boot_artifact_name.py`; create
`tests/providers/remote_libvirt/lifecycle/rootfs/test_boot_artifact_name.py`.

Interfaces:

- `BootArtifactKind = Literal["kernel", "initrd"]`.
- `BootArtifactName` frozen dataclass with the fields and `owner` property from the specification.
- `render_boot_artifact_name(kind, system_id, run_id, digest, *, attempt_id=None) -> str`.
- `parse_boot_artifact_name(name: str) -> BootArtifactName | None`.

Verification:

- Mode: focused-test. Canonical final/partial round trips, 203-byte ceiling, and all malformed
  classes fail before implementation; green command:
  `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/rootfs/test_boot_artifact_name.py -q`.

Steps:

1. Write the focused renderer/parser tests and observe import/test failure.
2. Implement one anchored regex, canonical UUID re-render checks, closed digest validation, and the
   renderer's state validation.
3. Run the focused command and expect all tests passed.

Acceptance: every parsed field comes only from a canonical version-1 name; every renderer output
round-trips and stays within the bound.

## Task 2 — Move materialization and the shared double onto the codec

Files: modify `boot_artifact_volumes.py`, `tests/providers/remote_libvirt/fakes.py`,
`test_fakes_storage.py`, and `test_boot_artifact_volumes.py`.

Interfaces:

- `artifact_volume_name(kind, system_id, run_id, payload_digest) -> str` remains the producer-facing
  wrapper, now backed by the codec.
- `render_boot_artifact_volume_xml(name, *, capacity_bytes) -> str` contains no ownership fields.
- Shared fake volumes support upload/download bytes; pools support clone, refresh, and enumeration;
  a connection/stream adapter presents the existing libvirt-shaped calls.

Verification:

- Mode: focused-test. Metadata-free creation, exact retry, different-digest coexistence, transfer
  cleanup, and shared-double readback fail against the old implementation; green command:
  `uv run python -m pytest tests/providers/remote_libvirt/test_boot_artifact_volumes.py tests/providers/remote_libvirt/test_fakes_storage.py -q`.

Steps:

1. Replace custom echo-oriented boot test doubles with the shared dir-pool double and add focused
   operation tests; observe metadata-free behavior fail.
2. Compute the digest before all names/lookups, use the codec for final/partial names, and remove
   metadata rendering/constants.
3. Implement only the fake methods driven by these tests and confirm the focused command passes.

Acceptance: materialization succeeds when XML metadata is discarded, and no test reads submitted
XML as storage readback.

## Task 3 — Parse and reap only proven objects

Files: modify `reaping/boot_artifacts.py` and
`tests/providers/remote_libvirt/reaping/test_boot_artifacts.py`.

Interfaces: `list_owned_boot_artifacts` and `reap_orphaned_boot_artifacts` retain their signatures;
`BootArtifactVolume` becomes the shared `BootArtifactName` representation or an exact alias.

Verification:

- Mode: focused-test. Metadata-free final/partial inventory and deletion plus malformed, foreign,
  digest-mismatch, unreadable, and live-owner retention fail against the old parser; green command:
  `uv run python -m pytest tests/providers/remote_libvirt/reaping/test_boot_artifacts.py -q`.

Steps:

1. Write shared-double tests for both successes and every fail-closed class; observe old inventory
   returns empty.
2. Remove XML parsing and parse the volume name before streaming bytes; preserve deletion/error
   contracts.
3. Run all three focused test files and expect all tests passed.

Acceptance: only canonical, content-matching, non-live version-1 artifacts can be deleted.

## Task 4 — Prove native behavior and close guardrails

Files: modify the existing remote external-boot live carrier only if its call sites require the new
digest-bearing name signature; no private environment file is committed.

Verification:

- Mode: focused-test. The native carrier must observe metadata-free readback, inventory two
  matching volumes after reconnect, reap exactly two, and observe an empty pool. Its command comes
  from the private environment mechanism already used by #2121.
- Mode: focused-test. Whole branch gates: `just lint`, `just type`, then bare captured `just ci`;
  expected exit 0.

Steps:

1. Run the native x86_64 carrier against the isolated remote pool and record only redacted facts;
   always execute its cleanup.
2. Run a controlled ownership/digest gate fault, observe the focused suite fail, and revert it.
3. Run lint/type/focused tests, adversarial review, security review, simplification, and full CI.

Acceptance: native remote libvirt recognizes/reaps both artifacts without metadata; the branch is
reviewed and CI-green at the exact pushed head.

Rollback: revert the PR. Version-1 objects then become foreign to the legacy reaper and remain
untouched; an operator may remove them after independent confirmation.
