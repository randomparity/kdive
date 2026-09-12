# 0645 — Make the installation guide the local-libvirt host-install authority

## Status

Proposed

## Context

The localhost Ansible playbook and `prepare-local-libvirt-host` recipe from #2393 are the
supported host-preparation implementation. The installation guide instead points operators to a
Debian/Ubuntu and RedHat-only example shell script, leaving the supported SUSE paths undiscoverable.

## Decision

`docs/operating/install.md` owns the operator procedure: set the witness DSN, run the canonical
recipe from a KDIVE checkout, start a new login session, and run the local preflight. It states
family limits and proof strength. The example script becomes a thin caller and its README retains
only the demo workflow plus a link to the installation guide for host preparation.

## Consequences

The guide must track recipe inputs and documented limits. It does not duplicate package mappings
or the emulator table; those remain owned by the playbook and platform-support guide.

## Considered & rejected

- **Keep the example script as the primary instructions.** verified: issue #2394 records that it
  refuses non-Debian/Ubuntu hosts while #2393's canonical recipe supports the six requested families.
- **Copy each family's package commands into the guide.** judgment: this would create another
  package-map implementation beside the Ansible role and its checked mappings.
