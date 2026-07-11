# Enabling KDIVE on ppc64le (Ubuntu 26.04)

**Date:** 2026-07-09
**Branch:** `feat/ppc64le-enablement`
**Goal:** A minimally-configured ppc64le Ubuntu 26.04 host and the existing x86 host both
run `just ci` successfully, with install docs updated for Ubuntu- and ppc64le-specific
requirements. All changes committed to the feature branch.

## Context

`just ci` is a fan-out gate chaining ~18 sub-recipes. Its real dependency surface:

| Concern | Tooling | ppc64le risk |
|---------|---------|--------------|
| lint / type | `uv`, `ruff`, `ty` (Astral) | prebuilt wheel/binary availability |
| CLI runner / hooks | `just`, `prek` (Rust) | prebuilt release-binary availability |
| python build deps | `libvirt-python`, `pydantic-core`, `psycopg[binary]`, `grpcio` | wheel availability / source build |
| lint-shell | `shfmt`, `shellcheck` | distro pkg / go build |
| lint-workflows | `zizmor` (Rust), `actionlint-py` (bundled Go bin) | wheel/binary availability |
| lint-ansible | `ansible-core`, `ansible-lint`, `yamllint`, `cryptography` | wheel availability |
| check-mermaid | `node` + npm deps | distro pkg |
| test | Docker (testcontainers Postgres/MinIO) | Docker CE apt repo for ppc64el |
| docs-* / config-* | stdlib python3 / `uv run` | none |

The pure-Python code is not the risk; **prebuilt-artifact availability on a tier-2/3 arch
is**. A Rust toolchain (rustup) is the source-build fallback engine.

The remote host inventory (probed 2026-07-09): Ubuntu 26.04 LTS ppc64el, Python 3.14.4
(matches `requires-python = "==3.14.*"`), `git` present, passwordless sudo. Everything
else (rust, uv, just, docker, gcc, libvirt, node) absent — a clean discovery surface.

## Approach

Empirical discovery loop, not a fixed edit list.

1. **Branch** off `main` (done: `feat/ppc64le-enablement`).
2. **Bootstrap the ppc64le host, correct methods only**, capturing each step:
   - `apt`: `build-essential`, `pkg-config`, `libvirt-dev`, `libelf-dev`, `shellcheck`,
     `nodejs`/`npm`, `curl`.
   - **Docker CE** from Docker's official apt repo (ppc64el).
   - **Rust via rustup** (`~/.cargo/bin` on PATH) — source-build engine.
   - **uv** via the astral installer; `just`/`prek` via `uv tool install` (source-build
     fallback if no wheel).
3. **Iterate `just ci` on the remote**, fixing each failure:
   - *Host setup* gap → captured in install docs + `check-setup-deps.sh` hints.
   - *Repo portability* gap (x86 assumption in a script/test/config) → committed code change.
4. **Re-run `just ci` on x86** to prove no regression in shared files.
5. **Update install docs** (`docs/operating/install.md`, README, `check-setup-deps.sh`)
   with Ubuntu- and ppc64le-specific requirements.

## Decisions

- **Wheel-less dependency escalation ladder:** (1) search alternate wheel providers —
  IBM Power wheel index (`https://wheels.developerfirst.ibm.com/power/linux`) and similar;
  (2) build from source with the Rust+gcc toolchain; (3) repin to a ppc64le-supporting
  version as a last resort, re-locking for both arches and flagging each such change.
- **Docs home:** `docs/operating/install.md` + `scripts/check-setup-deps.sh` hints
  (single canonical location, tied to `just check-deps`). No separate ppc64le doc.
- **Rust install:** rustup (official, adds `~/.cargo/bin`) over distro rust.
- **Docker install:** Docker CE from Docker's official apt repo, per the goal.

## Success criteria

- `just ci` exits 0 on the ppc64le remote host.
- `just ci` exits 0 on the local x86 host (no regression).
- `docs/operating/install.md`, README, and `check-setup-deps.sh` document the
  Ubuntu/ppc64le host prerequisites accurately.
- All changes committed to `feat/ppc64le-enablement`.

## Out of scope

- `live_vm` / `live_stack` suites (gated, excluded from `just ci`).
- ppc64le kernel build/boot/debug provider work (qemu-system-ppc64, guest images).
- CI matrix changes to actually run ppc64le in GitHub Actions (no ppc64le runners).
