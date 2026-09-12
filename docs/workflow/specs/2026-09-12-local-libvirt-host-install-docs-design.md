# Canonical local-libvirt host-install documentation

## Scope and authority

Issue #2394 and frozen token `q2394-013534aa` authorize this change. ADR-0645 records the
documentation ownership decision. The change excludes the playbook, recipe, package mappings, and
provider runtime behavior.

## Design

The installation guide becomes the first host-preparation destination. It names the recipe's
required DSN input, its privilege prompt, the new-login group boundary, and the follow-up preflight.
It lists all six families as structurally checked, with Ubuntu 26.04 as the only live-proven family
when an operator completes the requested apply; this checkout currently has no recorded live apply,
so the page must say that plainly. It calls out the RHEL/Rocky Python-minor guestfs limitation and
links the existing platform-support emulator table.

The example invokes the recipe instead of owning host mutation. Its README's first-run step points
to the guide and preserves its bring-up, image, token, and MCP-client walkthrough.

## Validation

Documentation link/path guards check references and anchors. A read-through checks that the guide's
commands correspond to the current `prepare-local-libvirt-host` recipe and its documented inputs.
