# drgn-live reads a staged DWARF vmlinux — truth and wiring (#3121, part 1)

Decision: [ADR-0723](../../adr/0723-drgn-live-reads-a-staged-dwarf-vmlinux.md). Issue: #3121.

## Problem

kdive tells agents and operators that in-guest drgn-live reads kernel BTF. No released drgn does
(ADR-0723 Context). The `live_drgn` signal, the `missing_debuginfo` warning, the runtime probe,
the `kdive-drgn` helper, the spine preflight, tool docstrings and guide pages all rest on that
claim. The live-script spine proof never uploads a `vmlinux`, so it cannot pass.

## Scope

In scope, one PR:

1. `src/kdive/images/drgn_support.py`, `src/kdive/images/cataloging/capability_signals.py`,
   `src/kdive/images/rootfs/catalog.py:59`, comments in `fixtures/local-libvirt/rootfs_catalog.toml`:
   remove `BTF_CAPABLE_DRGN`. A parsed version returns `capable` with `min_drgn_required=None`
   and a note that names the `vmlinux` upload. `not_applicable` and `unverified` do not change.
   `incapable` stays a valid value that nothing returns.
2. `src/kdive/kernel_config/gate.py`: `debuginfo_warning` warns when no `vmlinux` was uploaded
   and an effective config was uploaded (an external build), with `missing: ["vmlinux"]`.
   `debuginfo_unloadable_warning` keeps its reason and names `vmlinux`.
3. `src/kdive/mcp/tools/debug/introspection/{gate,live}.py`, `.../sessions/lifecycle.py`: the
   probe runs when the static warning is `None`; the uploaded-`vmlinux` skip goes away.
4. `src/kdive/kernel_config/requirements.py`: the `debuginfo` summary names the uploaded
   `vmlinux`; `bpf_tracing` loses `also_checked`. `also_checked_legend` and `ScopedEnforcement`
   stay as unused vocabulary (follow-up candidate). The published contract resource loses
   `bpf_tracing.also_checked`, its only instance.
5. `deploy/remote-libvirt-guest-helpers/kdive-drgn` and its README: `drgn -k` with no `-s`.
   Remove `KDIVE_BTF_PATH` from `src/kdive/config/external_env.py`; regenerate config docs.
6. `tests/integration/live_stack/spine.py`: `require_live_debug` requires a DWARF member, not
   `DEBUG_INFO_BTF`. `test_spine_live_script_over_the_wire` passes `with_vmlinux=True` and
   asserts no debuginfo warning on attach and on `introspect.script`.
7. Docs: `docs/guide/toolsets/{introspect,images}.md` and their `_content` copies;
   `docs/operating/external-build-upload.md` and its `_content` copy (`also_checked` text);
   `docs/operating/runbooks/live-testing.md:836`; `src/kdive/mcp/tools/catalog/images.py:245,403`
   docstrings, then `just docs` regenerates `docs/guide/reference/images.md`. Status lines on
   ADR-0322 (superseded) and amendment banners on ADR-0328, 0329, 0335 and 0548.

Out of scope, with owners: authority-path staging (#3123), remote-libvirt delivery (#3124),
guest free-space check (#3125), an upstream BTF reader (osandov/drgn#176), catalog version
drift (#3122), ppc64le vmcore translation (#2763).

## Failure model

1. **Actors and deployments:** an MCP agent calling `images.describe`, `debug.start_session`
   and `introspect.*`; an operator who uploads an external build; native x86_64 and ppc64le
   live tiers on local-libvirt and remote-libvirt.
2. **Invariants and assets:** the published `live_drgn` keys and values; the warning reason
   codes and the `{reason, missing, remediation}` shape; no silent blind session on an
   SSH-forward attach.
3. **Accepted failure classes:**
   - A drgn too old for the booted kernel's structures reports `capable` and passes the
     one-symbol probe. Owner of version evidence: #3122.
   - A Run with no uploaded effective config gets no static warning. The probe still runs.
   - An authority install with an upload is blind until #3123. The SSH-forward probe reports it.
   - A remote guest-agent attach with an upload is blind and silent until #3124 (no probe key).
   - A DWARF load longer than 10 s reports `debuginfo_unloadable` on a working session. The live
     proof records the probe latency.
4. **Covered elsewhere:** guest disk exhaustion (#3125); remote guest delivery (#3124).

## Success

1. Every `live_drgn` block with a parsed `drgn_version` has `capability: capable` and
   `min_drgn_required: null`.
2. `missing_debuginfo` and `debuginfo_unloadable` payloads name `vmlinux`, not `DEBUG_INFO_BTF`.
3. A drgn-live attach or `introspect.*` call with an uploaded `vmlinux` runs the probe.
4. `kdive-drgn` passes no `-s`. `rg -n -i 'KDIVE_BTF_PATH|reads BTF|in-guest BTF' src deploy
   docs/guide docs/operating` has no match.
5. On a native guest with the old helper, a staged `vmlinux` resolves `init_uts_ns`. ADR-0723
   records the drgn version, exit code and output before it is accepted. A failure stops the
   change for re-scoping.
6. `test_spine_live_script_over_the_wire` passes on native x86_64 through the local-libvirt
   legacy install path, with no debuginfo warning.

## Validation

Each task in the [plan](../plans/2026-10-06-drgn-live-dwarf-truth.md) names a focused test for
each changed contract. Success 5 and 6 are live proofs (plan Task 5).
