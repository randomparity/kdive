# Check the RHEL-family kdump set before the crash

Status: implementation design for #2762. Decision record: [ADR-0678](../../adr/0678-rhel-guest-kdump-advisory-keyed-on-image-os.md).

## Problem

`crash_capture_rhel_guest` is advertised in the external-build contract and read by nothing. A
kernel that passes the `crash_capture` gate but cannot mount Fedora's dracut kdump initramfs is
accepted at `runs.complete_build`, installs, crashes, and fails at `vmcore.fetch` with a bare
`readiness_failure` whose details carry only `system_id`.

## Design

**Family resolution** (`src/kdive/images/cataloging/catalog.py`). The System → registered public
catalog image lookup moves out of `mcp/tools/lifecycle/vmcore/_vmcore_kdump_gate.py`
(`_resolve_catalog_rootfs`) into `resolve_system_catalog_rootfs(conn, system) ->
ImageCatalogEntry | None`, unchanged in behaviour: local-libvirt `catalog` rootfs only, registered,
public, arch-matched; every gap is `None`. The kdump capability gate imports it; its private copy
and SQL are deleted. A new `image_os_id(entry) -> str | None` returns
`provenance["os_release"]["id"]` when it is a non-empty string.

**Advisory** (`src/kdive/kernel_config/gate.py`). `RHEL_FAMILY_OS_IDS = {"fedora", "rhel",
"centos", "rocky", "almalinux"}`. `rhel_guest_crash_warning(conn, run_id, *, os_id: str | None)`:

- `os_id` set and not in the set → `None` without reading the config;
- config absent/unreadable (`load_effective_config` is `None`) → `None`;
- `unmet_advertised_clauses(config, feature_requirement(CRASH_CAPTURE_RHEL_GUEST))` empty →
  `None`;
- otherwise `_clause_payload(..., reason="kernel_missing_rhel_guest_crash_config")` plus
  `guest_family: "rhel"` (known id) or `"unknown"` (`os_id is None`), each with its own
  remediation; the `unknown` text states the warning applies only if the guest is RHEL-family.

**Envelope** (`runs/complete_build.py`). `_success_envelope` resolves `os_id`: `None` when
`run.system_id` is `None`, the System row is missing, the resolver returns `None`, or the lookup
raises `psycopg.Error`/`ValidationError` inside a savepoint (logged; the Run already committed and
replays recompute this, so it fails open to `unknown` and leaves the transaction usable); else `image_os_id(entry)`. It adds `data["rhel_guest_crash_config"]` and the contract ref when the
warning is non-`None`. The nudge stays exclusive with both warnings; the boot warning and this one
are independent. The recorded-result replay path recomputes through the same method. The
`runs.complete_build` wrapper docstring names the field. `crash_capture_rhel_guest.enforcement`
becomes `UPLOAD_ADVISORY` and its summary drops "kdive cannot tell which OS your guest runs".

**Capture hint.** `requirements.EMPTY_CAPTURE_CONFIG_HINT` is one string naming
`crash_capture_rhel_guest`, `data.rhel_guest_crash_config` and the contract. It is a string
because the job worker keeps only scalar details (`jobs/worker.py::_safe_detail`); the agent reads
it as `failure_detail_kernel_config_hint`. It opens "kdive found no kdump core", true on both
remote paths (no core in dump storage; agent never returned).
Local `LocalLibvirtRetrieve._no_core(system_id, method)` adds it as
`details["kernel_config_hint"]` unless `method is CaptureMethod.HOST_DUMP`. Remote
`common.readiness_failure(system_id, reason, *, config_hint=False)` adds it when `True`; both
readiness failures in `remote_libvirt/retrieve/kdump_capture.py` pass `True`.

**Spine.** `build_and_upload_kernel` declares and PUTs the bytes `check_spine_kernel_config`
already returned as `effective_config`, guarded like `kernel`. Live callers that reserve a
crashkernel now meet `crash_capture_refusal` and must pass `require_kdump=True`.

**Docs.** `docs/operating/external-build-upload.md` states the new advisory and hint; the packaged
copy is regenerated (`just resources-docs`), as are the generated tool reference and CLI verbs.

## Failure model

1. Actors and deployments: an authenticated agent calling `runs.complete_build` and `vmcore.fetch`
   on the portable host stack with local-libvirt or remote-libvirt Systems.
2. Invariants at stake: `complete_build` never refuses over config (ADR-0330); the ADR-0330
   `{reason, missing, remediation}` payload shape; the nudge's exclusivity (ADR-0398); the kdump
   capability gate's behaviour through the moved resolver.
3. Accepted failure classes:
   - `unknown` warnings on non-RHEL guests kdive cannot identify — stated in the remediation
     (ADR-0678 Consequences).
   - Remote-libvirt Systems are always `unknown` — the capture hint covers them.
   - The hint appears on a no-core failure whose real cause is not the config — it is phrased as
     a likely cause, not a diagnosis.
   - A second config read per completion — bounded by the 1 MiB `effective_config` limit.
   - A family-lookup error reports `unknown` rather than failing the committed completion.
4. Covered elsewhere: refusing on this set and the `{KEXEC, KEXEC_FILE}` gate (ADR-0478 §1/§3);
   comparing against the image `.config` sibling, debian/suse sets (operator exclusions).

## Success and validation

| Criterion | Evidence |
|---|---|
| RHEL id + config lacking `SQUASHFS_ZSTD` → warning names it, `guest_family: "rhel"` | focused-test `tests/kernel_config/test_gate.py` |
| non-RHEL id silent without reading config; complete config silent; absent config silent | focused-test `tests/kernel_config/test_gate.py` |
| unresolved id → `guest_family: "unknown"` | focused-test `tests/kernel_config/test_gate.py` |
| envelope carries the field for bound and unbound Runs, beside `missing_boot_config`, absent with the nudge; lookup error → `unknown` | focused-test `tests/mcp/lifecycle/test_complete_build_tool.py` |
| resolver moved unchanged; `image_os_id` shapes | focused-test `tests/images/test_catalog_resolver.py`, `tests/mcp/tools/test_vmcore_kdump_gate.py` |
| local/remote no-core details carry the hint and it survives `_failure_context`; host_dump does not | focused-test `tests/providers/local_libvirt/test_retrieve_kdump.py`, `tests/providers/remote_libvirt/retrieve/test_retrieve.py` |
| served contract and registry guard show `upload_advisory` | focused-test `tests/mcp/catalog/test_external_build_contract_resource.py`, `tests/kernel_config/test_requirements.py` |
| spine uploads `effective_config` | focused-test `tests/integration/live_stack/test_spine.py` |
| docs and generated copies | `just resources-docs-check` and the generated-doc checks in `just ci` |

Live arm (Fedora 44 host): `images.describe` on the System's image reports `os.id == "fedora"`;
then `runs.complete_build` for a Run bound to that System, uploading a `.config` without
`SQUASHFS_ZSTD`, returns `data.rhel_guest_crash_config` with `guest_family == "rhel"` and
`SQUASHFS_ZSTD` in `missing`. Negative (Ubuntu 26.04 host, Debian-family image): the key is absent.
