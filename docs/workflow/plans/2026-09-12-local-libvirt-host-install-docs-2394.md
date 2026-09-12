# Local-libvirt host-install documentation plan

Goal: make the installation guide the canonical operator procedure for the existing localhost
host-preparation recipe.

Architecture: documentation directs privileged setup to the existing Ansible-backed `just` recipe;
the example delegates to that same entry point. No package or runtime implementation changes.

Tech stack: Markdown, Bash, `just`, documentation guard scripts.

## Global Constraints

- Preserve the frozen #2394 surface and do not change host-preparation implementation.
- State proof strength without presenting checked families as live-proven.
- Reuse the platform-support emulator table rather than copying package mappings.

Expected implementation size: 90–150 changed lines (M) — one guide section, a thin example caller,
and focused README replacement.

## Task 1 — Publish the canonical guide

Files: modify `docs/operating/install.md`.

Interfaces: consumes `just prepare-local-libvirt-host`,
`KDIVE_LIFECYCLE_WITNESS_DATABASE_URL`, and `just check-local-libvirt`; later example text links
to this guide.

Verification:

- Contract: local operators can find the exact host-install procedure and family limits. Mode:
  task-test-not-applicable — prose has no independent executable consumer; the link guards below
  validate referenced paths, while a manual read-through verifies command correspondence.

1. Replace the delegation with the recipe, its required environment input, privilege behavior,
   relogin requirement, preflight, family support/proof table, guestfs limitation, and emulator-table
   link.
2. Run `just docs-links`, `just docs-paths`, and `just served-doc-links`; expect each to exit zero.

## Task 2 — Delegate the example

Files: modify `examples/local-libvirt/install-host.sh` and `examples/local-libvirt/README.md`.

Interfaces: consumes the guide URL and canonical `just` recipe; the example's later `up.sh`,
`build-image.sh`, and token workflow remain unchanged.

Verification:

- Contract: the example has one host-preparation implementation path. Mode:
  focused-test — inspect the script's shell syntax with `bash -n examples/local-libvirt/install-host.sh`;
  before replacement the old script contains package implementation, after replacement it is a valid
  delegating caller.

1. Replace the script with an argument-preserving `just prepare-local-libvirt-host` caller.
2. Point the README prerequisite and first usage step to the canonical guide, retaining the demo
   workflow.
3. Run `bash -n examples/local-libvirt/install-host.sh`; expect exit zero.
