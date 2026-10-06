# drgn-live reads a staged DWARF vmlinux — truth and wiring (#3121, part 1)

Decision: [ADR-0723](../../adr/0723-drgn-live-reads-a-staged-dwarf-vmlinux.md). Issue: #3121.

## Problem

kdive tells agents and operators that in-guest drgn-live reads kernel BTF. No released drgn does
(ADR-0723 Context). The `live_drgn` signal, the `missing_debuginfo` warning, the runtime probe,
the `kdive-drgn` helper, the spine preflight and six guide pages all rest on that claim. The
live-script spine proof never uploads a `vmlinux`, so it cannot pass.

## Scope

In scope, one PR:

1. `src/kdive/images/drgn_support.py`, `src/kdive/images/cataloging/capability_signals.py`:
   remove `BTF_CAPABLE_DRGN`. A parsed version returns `capable` with `min_drgn_required=None`
   and the note `_VMLINUX_NOTE`. `not_applicable` and `unverified` do not change. `incapable`
   stays a valid value that nothing returns.
2. `src/kdive/kernel_config/gate.py`: `debuginfo_warning` warns when no `vmlinux` was uploaded
   and an effective config was uploaded (an external build). The payload is
   `{reason: "missing_debuginfo", missing: ["vmlinux"], remediation}`. `debuginfo_unloadable_warning`
   keeps its reason, names `vmlinux`, and says that the guest has no matching DWARF `vmlinux`.
3. `src/kdive/mcp/tools/debug/introspection/gate.py`, `.../introspection/live.py`,
   `.../sessions/lifecycle.py`: the probe runs when the static warning is `None`. The
   `has_uploaded_vmlinux` parameter and the `debuginfo_ref` skip go away.
4. `src/kdive/kernel_config/requirements.py`: the `debuginfo` summary says drgn-live reads the
   uploaded `vmlinux`. `bpf_tracing` loses its `also_checked` entry, because no seam reads BTF.
5. `deploy/remote-libvirt-guest-helpers/kdive-drgn` and its README: run `drgn -k` with no `-s`.
   Remove `KDIVE_BTF_PATH` from `src/kdive/config/external_env.py` and regenerate the config docs.
6. `tests/integration/live_stack/spine.py`: `require_live_debug` requires a DWARF member, not
   `DEBUG_INFO_BTF`. `test_spine_live_script_over_the_wire` passes `with_vmlinux=True`.
7. Docs: `docs/guide/toolsets/introspect.md`, `docs/guide/toolsets/images.md`,
   `docs/guide/reference/images.md` and their `src/kdive/mcp/resources/_content/` copies.
   Status lines on ADR-0322 (superseded) and amendment banners on ADR-0328, 0329 and 0335.

Out of scope, with owners: authority-path staging (#3123), remote-libvirt delivery (#3124),
guest free-space check (#3125), an upstream BTF reader (osandov/drgn#176), catalog version
drift (#3122), ppc64le vmcore translation (#2763).

## Failure model

1. **Actors and deployments:** an MCP agent calling `images.describe`, `debug.start_session`
   and `introspect.*`; an operator who uploads an external build; the native x86_64 and ppc64le
   live tiers.
2. **Invariants and assets:** the published `live_drgn` keys and values; the warning reason
   codes and the `{reason, missing, remediation}` shape; no new false "capable" or silent blind
   session on the local legacy path.
3. **Accepted failure classes:**
   - An image whose drgn is too old for the booted kernel reports `capable`. The always-on
     probe reports `debuginfo_unloadable` at attach (ADR-0723 Consequences).
   - A Run with no uploaded effective config gets no static warning. The probe still runs.
   - An authority or remote install with an upload stays blind until #3123 or #3124. The probe
     reports it.
4. **Covered elsewhere:** guest disk exhaustion (#3125); remote guest delivery (#3124).

## Success

1. Every `live_drgn` block with a parsed `drgn_version` has `capability: capable` and
   `min_drgn_required: null`.
2. `missing_debuginfo` and `debuginfo_unloadable` payloads name `vmlinux` and never
   `DEBUG_INFO_BTF`.
3. A drgn-live attach or `introspect.*` call with an uploaded `vmlinux` runs the probe.
4. `kdive-drgn` passes no `-s`, and no file in `src/`, `deploy/` or `docs/guide/` names
   `KDIVE_BTF_PATH` or says drgn-live reads BTF.
5. On a native guest with the old helper, the staged `vmlinux` resolves `init_uts_ns`.
6. `test_spine_live_script_over_the_wire` passes on one native local-libvirt host (x86_64 or
   ppc64le) whose stack installs through the legacy path, not the authority.

## Validation

Each task in the [plan](../plans/2026-10-06-drgn-live-dwarf-truth.md) names a focused test for
each changed contract. Success 5 and 6 are live proofs (plan Task 5).
