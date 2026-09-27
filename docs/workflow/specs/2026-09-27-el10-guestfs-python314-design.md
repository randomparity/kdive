# EL10 Python 3.14 libguestfs binding

## Problem

EL10 packages Python 3.14 in AppStream but packages `python3-libguestfs` for the system
Python 3.12. The lifecycle worker requires Python 3.14 and imports `guestfs` while
provisioning, building a rootfs, staging a kernel, external booting, and retrieving a local
vmcore. The installer recreates its venv, so a manually copied binding does not survive.

## Scope

For RHEL-compatible EL10 hosts admitted by `local_worker_host`, install Python 3.14 build
headers and libguestfs headers; obtain the distribution's signed libguestfs source RPM for
the exact installed `libguestfs` source NEVR; compile its generated Python binding sources
against the installed C library and Python 3.14; and make the result importable from the
base interpreter. The existing lifecycle installer links that binding into each fresh venv
and verifies the import. The build stays under a root-owned versioned directory, outside
the replaced venv. It does not install a second libguestfs runtime or appliance.

Source download uses enabled EL source repository definitions. The source RPM must pass RPM
signature verification and match both `libguestfs` and `libguestfs-devel` source NEVRs.
The installed upstream source tarball's version must match the installed RPM version.
Build failure, source mismatch, absent repository, or failed base import stops host
preparation with an actionable error. Do not silently mark the host ready.

The selected generated C wrappers may omit distribution patch-only API additions. KDIVE
uses the upstream API; a changed source layout or binding incompatibility fails the
compile/import gate and requires a reviewed update. This is narrower than rebuilding
the entire libguestfs package and appliance.

The support table remains the canonical host/guest mapping. EL10 worker support is
recorded as provisioned only for releases whose binding build and live provisioning arm
actually pass. Other admitted EL releases remain conditional pending matching proof.

The approved exclusions are guestfs-consuming provider logic, SLES/openSUSE binding
routes, the EL btrfs appliance gap, and Ubuntu/Fedora binding routes.

### Failure model

| Class | Result |
|---|---|
| Missing source repo, headers, or trusted source package | Host preparation fails with the missing prerequisite and no readiness claim. |
| Installed libguestfs or source version changes | The binding is rebuilt from the exact current source; a failed rebuild stops preparation. |
| Compilation or import fails | No new binding is published; the worker installer fails loud. |
| Running workers during a host upgrade | Existing workers keep their imported code until the operator restarts them; host preparation does not promise a rolling upgrade. |

## Success

- On a clean Rocky 10 host with the documented repositories, preparation supplies a
  Python 3.14 binding outside the lifecycle venv; the base interpreter and a freshly
  installed worker venv import it.
- Repeated preparation reuses the matching verified binding. A changed libguestfs source
  NEVR selects a new build and does not silently reuse the prior binding.
- A live Rocky 10 KVM host launches libguestfs and completes the relevant KDIVE
  provisioning or rootfs-build path before the docs claim that tested configuration.
- Fedora and Ubuntu continue using their packaged bindings.

## Validation

- `focused-test`: a controlled EL10 container builds from its matching signed source RPM,
  imports with Python 3.14, and repeats after deleting/recreating a venv.
- `focused-test`: installer tests cover the base-interpreter-to-venv link and loud failure.
- `focused-test`: Ansible task tests cover EL10-only prerequisites and ordering.
- `focused-test`: a live Rocky 10 KVM VM runs the built binding and KDIVE provision/rootfs
  path; record candidate SHA, distribution, libguestfs source NEVR, and exact result.
- `task-test-not-applicable`: documentation wording is human-facing and has no executable
  semantic parser; run link/path guards, then read the table against source evidence.

Decision: [ADR-0692](../../adr/0692-build-el10-python314-libguestfs-binding.md).
