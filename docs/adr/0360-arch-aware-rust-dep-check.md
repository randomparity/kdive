# ADR 0360 — Arch-aware Rust-toolchain host-dependency check

- **Status:** Accepted
- **Date:** 2026-07-15
- **Issue:** #1186
- **Epic:** #1189 (cross-platform dev tooling)
- **Related:** ADR-0338/0352/0353 (the arch-awareness the dep-checker already carries —
  `SUPPORTED_ARCHES`, per-arch qemu probes, the cross-arch advisory from #1153)

## Context

`scripts/check-setup-deps.sh` (`just check-deps`) reports the host packages a developer
needs before `uv sync`, grouped by tier with a per-distro install hint. It is already
arch-aware for QEMU: it captures the host arch (`uname -m`), maps each supported arch to
its emulator binary, and prints a cross-arch guest advisory (#1153).

It has no Rust-toolchain check. On arches for which PyPI ships no prebuilt wheels — today
`ppc64le` — the Python extension dependencies (`pydantic-core`, and the `just`/`prek`
tools) build from source, which requires `rustc`/`cargo` on `PATH`. Without them, a
ppc64le developer hits an opaque `uv sync` compile failure instead of an actionable
prerequisite message. `AGENTS.md`/`README.md` already state the requirement in prose
(rustup), but the checker does not detect it, so the requirement is unenforced.

x86_64 gets prebuilt wheels for every extension dependency, so it must **not** acquire a
false Rust requirement.

## Decision

Introduce a **wheel-less-arch dependency class**: a required host tool needed only on
arches PyPI publishes no wheels for. Encode the set as `WHEELLESS_ARCHES=(ppc64le)` with
an `arch_needs_rust` membership check mirroring the existing `arch_is_supported` helper.

In the REQUIRED tier, when the host arch is wheel-less, require a Rust toolchain: probe
`rustc` **and** `cargo` on `PATH` and, if either is missing, emit a single manual hint
routing to rustup (`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`).
Rust is routed through the manual-hint channel (`note_manual`), not the distro
package-manager line, because the project mandates rustup (matching `just`/`prek`), not a
distro `rust` package. A missing toolchain therefore joins the existing required-tier
failure and exits 1 with the fix; a present toolchain is silent.

An empty or unknown host arch (e.g. a restricted-PATH environment with no `uname`) is
treated as wheel-ful — no Rust requirement — so the existing distro-hint tests, which run
with an empty PATH and no `uname` stub, keep their behavior and no false requirement is
raised where the arch cannot be determined.

The existing `libvirt-dev`/`python3-dev` header checks stay unconditional across all
arches. x86_64 is unchanged.

## Consequences

- ppc64le developers get an actionable rustup hint at `just check-deps` time instead of a
  source-build failure; the prose requirement is now enforced by the checker.
- x86_64 behavior is byte-for-byte unchanged.
- Adding a future wheel-less arch is a one-line edit to `WHEELLESS_ARCHES`.
- The requirement lives only in the dev-host checker; it is not a runtime/production
  dependency (production installs from wheels or a built image).

## Rejected alternatives

- **A distro `rust` package hint** (route through `package_for`/the package-manager line):
  the project standardizes on rustup so the toolchain matches across distros and tracks a
  current stable, exactly as `just`/`prek` already do.
- **Two separate `rustc` and `cargo` manual hints:** both are provided by the one rustup
  install, so two identical rustup lines would be noise; one combined `rustc/cargo` probe
  emits a single hint.
- **Requiring Rust on every arch:** x86_64 has wheels; a Rust requirement there is a false
  prerequisite that would fail clean x86_64 setups.
- **Failing when the host arch is unknown/empty:** would raise a spurious Rust requirement
  in restricted-PATH environments that cannot run `uname`; wheel-ful-by-default is safe
  because the only wheel-less arch is explicitly enumerated.
