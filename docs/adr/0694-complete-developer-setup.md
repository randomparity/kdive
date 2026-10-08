# 0694 Complete developer setup

## Status

Accepted (2026-09-27)

Partially superseded by [ADR-0732](0732-checkout-scoped-developer-shfmt.md) for shfmt
installation and developer-check selection only.

Supersedes ADR-0393 and ADR-0673 only for the `just setup` installation path.
Their report-only and host-tool requirements remain applicable elsewhere.

## Context

Setup could finish without tools required by the local CI gate. Missing hook dependencies
were reported, but developers still had to discover and install the wider toolchain manually.
Repository users and developers need different dependency contracts.

## Decision

`just setup` invokes an explicit developer installer before syncing Python dependencies.
It installs native development libraries, compiler tools, Docker and Compose, pinned hook
and lint CLIs, and promtool. The existing recipe then syncs the locked environment, stages
the capture manifest, installs Ansible collections and hooks, and runs commit hooks.
System packages use the detected distribution's package manager; user binaries use uv's
tool directory, which must already be on PATH. Release archives are checked against
committed SHA-256 digests. Go tools build for the host; POWER builds ShellCheck with Cabal.

Git, Bash >= 4.4, uv, and just are bootstrap prerequisites. Full developer setup targets
Linux. A failed install, unavailable required tool, or inaccessible Docker daemon stops
setup. Docker service startup, access policy and external repository configuration remain
operator actions; failures name the remedy rather than claiming developer readiness.

The ordinary `just check-deps` report retains its existing behavior. `--setup` is a
read-only developer preflight; `--install-developer` explicitly installs its requirements.
Live VM host provisioning, guest images and worker services remain separate deployment
operations and are not required to edit, lint, type-check and run the ordinary suite.

## Consequences

Setup writes system packages and user tools outside the checkout and requires package
installation privileges when packages are missing. Initial native tool builds take longer,
particularly on POWER. Reruns reuse installed tools. Tests exercise installation order,
failure propagation, archive verification and the POWER source-build path.

## Considered & rejected

- Require manual tool installation before setup. judgment: leaves the developer dependency
  contract spread across documentation and late command failures.
- Install live VM services and images on every developer host. judgment: ordinary tests need
  disposable containers, while VM provisioning requires a separately configured host contract.
