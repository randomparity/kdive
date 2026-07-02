# Provisioning-for-debugging in the agent workflow guides (#955)

- Issue: [#955](https://github.com/randomparity/kdive/issues/955)
- Builds on: [ADR-0284](../adr/0284-agent-facing-workflow-docs.md) (agent-facing
  workflow doc system). Content addition within that system; no new architectural
  decision, so no new ADR.
- Date: 2026-07-02

## Problem

The agent-facing workflow guides (`docs/guide/agent-index.md` plus the linked
`docs/guide/toolsets/*.md`, served as MCP doc resources per ADR-0284) map a session from
orient → provision → build → boot → observe → debug → triage → release. They omit the
**provision-time** decisions a debugging or live-introspection session depends on.

Several capture and introspection methods are bound at `systems.provision` and cannot be
enabled on an already-ready System. An agent that discovers the need mid-session — for
example, decides to attach a debugger only after a run boots — cannot flip the knob; it
must tear down and reprovision (→ rebuild → reboot), a full expensive cycle. The guides
give the agent no signal to make these choices up front.

The provision-bound knobs, verified against source:

- **`provider.local-libvirt.debug.gdbstub`** — defaults `False`
  (`src/kdive/profiles/provisioning.py`, `LibvirtDebugOptions.gdbstub`). The gdbstub port
  is allocated only at provision (`.../lifecycle/provisioning.py`), so
  `debug.start_session` against a System not provisioned for it raises
  `configuration_error` — "System … was not provisioned with a gdbstub; reprovision with
  the profile's debug.gdbstub set" (`.../lifecycle/connect.py`).
- **`provider.local-libvirt.debug.preserve_on_crash`** — same provision-bound
  `LibvirtDebugOptions`; adds the pvpanic device and `<on_crash>preserve</on_crash>` so a
  crashed guest is held for live post-crash attach rather than destroyed.
- **`provider.local-libvirt.ssh_credential_ref`** — the live-ssh transport that
  drgn-over-SSH live introspection (`introspect.run`, `introspect.script`) rides resolves
  the guest credential through this profile reference (`.../profile_policy.py`,
  `src/kdive/images/capability_signals.py`). A profile that does not opt into live
  introspection leaves it `None`, and the transport has no credential to reach the guest.

## Goals

- Give the agent, in the workflow guides it already reads, a clear up-front signal that
  these knobs are bound at provision and cost a reprovision cycle if deferred.
- State each knob as a concrete profile field the agent sets at `systems.provision`, with
  the tool/method it unlocks and the failure it prevents.
- Keep the guidance discoverable from both the workflow map (`agent-index.md`) and the
  two toolset guides an agent lands on when it hits the trap (`debug.md`, `systems.md`).

## Non-goals

- **No behavior change.** This is documentation only. Enabling gdbstub on a ready System
  without a reprovision would be a separate feature (the issue says so explicitly); this
  issue is scoped to the docs.
- **No new tool, schema, or data-contract change.** The provisioning knobs already exist;
  only the agent-facing prose changes.
- **No operator-doc change.** The issue title scopes this to the *agent workflow guides*.
  `docs/operating/providers/local-libvirt-walkthrough.md` is operator-facing; leaving it
  is a deliberate scope boundary, noted as possible follow-up.

## Decision

Add a short **"Provisioning for debugging and live introspection"** section to
`docs/guide/agent-index.md`, immediately after the typical-session list (where the
provision stage is introduced). It states the general rule (these are bound at provision;
decide before `systems.provision` or pay a reprovision cycle) and lists the three knobs
with what each unlocks.

Reinforce it at the two points an agent lands when it hits the trap:

- **`docs/guide/toolsets/debug.md`** — a short note that a live GDB session requires the
  System to have been provisioned with the profile's `debug` section `gdbstub: true`;
  otherwise `debug.start_session` fails `configuration_error` and the only remedy is
  reprovision. Points back to the index section.
- **`docs/guide/toolsets/systems.md`** — a short note in the provisioning subsection that
  the `debug` flags and live-ssh credential are bound at provision and cannot be added to
  a ready System, cross-referencing the index section.

Canonical docs are edited under `docs/`; the served snapshots in
`src/kdive/mcp/resources/_content/` are regenerated with `just resources-docs` and drift
is guarded by `resources-docs-check`.

### Wording constraint (completeness guard)

`tests/mcp/resources/test_toolset_doc_completeness.py` greps each toolset doc for
`\b<ns>\.[a-z_]+` and fails if a matched token is not a live tool. So the profile field
must **not** be written as the dotted `debug.gdbstub` form in a namespace-matching doc —
it would be mis-read as a stale `debug.*` tool. It is written as the profile's `debug`
section with `gdbstub: true` / `preserve_on_crash: true`. In `agent-index.md` the full
profile path `provider.local-libvirt.debug.gdbstub` is safe (the backticked-tool regex
`` `[a-z_]+\.[a-z_]+` `` does not match a hyphenated multi-dot token), and every
backticked `ns.tool` used there (`debug.start_session`, `systems.provision`,
`systems.reprovision`, `introspect.run`) is a live tool.

## Considered & rejected

- **Document fault-injection provisioning in the agent guides.** The issue's phrasing
  ("fault-injection kernel-config") suggests it, but in this codebase `fault-inject` is a
  mock **provider** deliberately hidden from the agent surface (ADR-0269/0270, #879/#880;
  `test_no_adr_leak` and the schema projection keep it off the wire). Documenting its
  provisioning in an agent-facing doc would reverse that settled decision and reintroduce
  a surface the platform intentionally hides. Scoped out. (Fault injection as a *kernel
  build config* is a `buildconfig` concern, not a provision-time knob, and is out of this
  issue's provisioning frame either way.)
- **Add a `debug_note` field to `systems.profile_examples` output.** The issue offers
  "agent-index and/or profile_examples notes" as alternatives. Adding a field to the
  tool's returned `data` is an agent-facing data-contract change that cascades into the
  wrapper docstring, the generated tool-reference doc, and new tests — disproportionate
  for a priority:low docs issue when the index section already closes the gap in the
  guide the agent reads. Left as a natural point-of-use follow-up.
- **A new `introspect` toolset guide.** ADR-0284 phases the `introspect` toolset doc into
  a later batch; minting it here would exceed this issue and duplicate that plan. The
  index section names live introspection and its provision prerequisite without a full
  new toolset doc.
- **Amend the operator walkthrough too.** Out of the issue's "agent workflow guides"
  scope; noted as follow-up rather than folded in to avoid scope creep.
