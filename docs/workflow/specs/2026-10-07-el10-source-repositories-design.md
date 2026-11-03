# EL10 guestfs source repository selection

## Problem and authority

Issue #3068 requires the EL10 binding builder to fetch the installed distribution's
libguestfs source without activating unrelated third-party source repositories.
The frozen scope is issue comment 6047265212, token `q3068-3090bc30`.
Human-approved exclusions remain UV installation (#3069) and tool policy (#3070).
This is a full-spec, S change with fixed denominator 100; it does not qualify POWER.

On Rocky 10.2 with Docker's official RHEL repository definition, the current
`dnf download --source --enablerepo='*source*'` fails fetching
`docker-ce-stable-source` metadata. A live exact-source query restricted to
`appstream-source` succeeds. DNF 4.20 also automatically enables matching source
repositories for enabled binary repositories, so removing the wildcard alone
would leave the same dependency on third-party metadata.

## Design

Retain the existing privileged builder, Ansible caller, signed-source checks,
compilation, publication and reuse path from ADR-0692. No ownership migration,
new interface, dependency, or architectural decision is required.

Only on the build path, select the distribution's AppStream source repository:
`appstream-source` for Rocky/AlmaLinux; `rhel-10-for-<rpm-arch>-appstream-source-rpms`
for RHEL. Read the distribution ID from `/etc/os-release` in a subshell and the
RHEL architecture from RPM. Use DNF `--repo` with this exact ID to disable other
repositories for that invocation; retain `--source`, the installed source NEVR,
and destination. No persistent repository configuration is changed.

An unknown distribution fails before downloading with an actionable error.
A missing source definition, unavailable metadata, or missing exact source RPM
continues to fail preparation. Do not fall back to broader repository matching.
Existing source signature and binary/devel identity checks remain mandatory.
This confines both Docker-repository and podman-docker paths identically;
container-engine installation and socket policy remain operator responsibilities.

Alternatives: removing only the wildcard retains DNF's implicit source activation
(verified against installed DNF source); dynamically discovering source ownership
adds package/repository mapping complexity when the documented requirement is the
standard AppStream definition (judgment). Explicit distro selection is the bounded
correction to the existing distribution-source contract.

## Failure model

- **Actors and deployments:** operators running the existing root EL10 binding
  builder on Rocky, AlmaLinux, or subscribed RHEL; x86_64 live proof and preserved
  ppc64le selection behavior.
- **Invariants and assets at stake:** no third-party metadata participates in source
  acquisition; exact source identity/signature verification precedes privileged
  compilation and publication; a failure must not publish a binding.
- **Accepted failure classes:** unavailable standard source definition or exact
  source version stops preparation; an administrator replacing the standard repo
  definition remains within existing root-controlled DNF/RPM trust, with the
  existing signature identity checks retained. No alternate private repo mapping
  is added by this change.
- **Covered elsewhere:** UV installation #3069; tool policy #3070; native POWER
  qualification #2818; existing signature/import/atomic publication safeguards
  remain ADR-0692 responsibilities.

## Success and validation

The script enables exactly the selected distro source repo, including when a
third-party source is enabled by the operator. Tests execute the actual shell
source-acquisition block with mocked command boundaries for Rocky, AlmaLinux,
RHEL x86_64 and ppc64le, plus absent source/unsupported distro failures. They
assert exact NEVR/destination preservation and nonzero failure propagation.

Live evidence first reproduces the reported failure, then runs candidate source
acquisition and the binding builder on the disposable Rocky host. Rerun the
enterprise host-install cell on a clean baseline after dependencies land; a
failure owned by #3069 or #3070 remains a failed cell and a release blocker.
Update direct operator guidance to describe invocation-only AppStream selection
and retain truthful host-install status. Run focused tests, lint/type/shell gates,
and the installed pre-push CI gate. No full suite is duplicated for design prose.
