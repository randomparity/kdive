# 0692 — Build the EL10 Python 3.14 libguestfs binding from matching source

## Status

Accepted (2026-09-27)

## Context

EL10's AppStream provides Python 3.14, but its `python3-libguestfs` extension targets
the system Python 3.12. KDIVE's lifecycle worker runs 3.14 and requires `guestfs`
throughout its local-libvirt provisioning path. The worker installer recreates its
venv, so the binding must live outside it. Issue #2784 and the operator's 2026-09-27
decision authorize a matched-source build, subject to live EL10 proof.

## Decision

`local_worker_host` prepares the EL10 build prerequisites and invokes a focused binding
builder before the lifecycle worker installer. The builder obtains the distribution's
signed source RPM matching the installed `libguestfs` and `libguestfs-devel` source
NEVR, compiles its generated Python sources against those installed headers and
Python 3.14, and publishes the binding in a root-owned versioned directory. A Python
site path entry makes it importable by the base interpreter. The existing installer
then links the binding into its freshly recreated venv and validates the import.

The builder never replaces the distro C library or appliance and never publishes
an unverified binding. It fails preparation if source lookup, signature, match,
compile, or import fails. The supported-host claim requires an observed live EL10
provisioning result on the candidate revision.

## Consequences

EL10 host preparation needs the distro's source repository and CRB/CodeReady Builder
headers, plus a C compiler. A libguestfs RPM update changes the selected source NEVR
and requires a new build. Old workers continue using their loaded extension until
restart; upgrades are not rolling. Distribution patches adding Python API wrappers
outside upstream generated sources are not exposed by this binding. A source layout
change fails closed and needs a reviewed builder change.

## Considered & rejected

- **Run the worker on EL10's Python 3.12.** verified: `pyproject.toml` at
  `7a2efdfe8` requires Python `==3.14.*`; this would change the project toolchain
  and locked runtime dependencies for one host family.
- **Use uv to fetch the binding.** verified: upstream's [Python binding manual](
  https://www.libguestfs.org/guestfs-python.3.html) states that current bindings
  are not on PyPI; uv cannot synthesize the missing native extension.
- **Build all of libguestfs from source.** judgment: replacing the distribution's
  runtime and appliance widens the host contract when only a Python ABI extension
  is missing.
- **Keep EL10 blocked.** judgment: the matching-source build is small enough to
  verify directly on a Rocky 10 host; it should be attempted before removing host
  support.
