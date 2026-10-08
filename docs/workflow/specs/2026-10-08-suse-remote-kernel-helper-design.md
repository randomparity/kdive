# SUSE remote image and kernel helper

## Scope and source

Status: approved geometry amendment prepared for independent design review and scope audit; no growth implementation authorized yet.

Implement #3082 under ADR-0767 and ADR-0768. Frozen amended charter6067241872,
retained token q3082-1f7d293a. Original scope/exclusions, the optional checksum input, native Wicked amendment
and exact pinned-Leap offline growth proposal are operator-approved. Together these
add native lease lifecycle/return routing and sufficient build geometry to the
same canonical image outcome. Debian
#3081, wider distribution coverage and native POWER #2818 remain excluded.

## Verified cause

The original baseline catalog has no SUSE row; a direct catalog assertion fails. The RHEL
helper requires grubby; main now has a separate Debian variant and family selector. Downloaded official Leap 15.6 Build19.146 bytes match the
local catalog SHA-256 `0a5720416d423f98aacaa793a57d56ec045e3dd25cd88713952660ad00da53bd`.
Read-only inspection finds GRUB2, dracut, XFS root, persistent `GRUB_DEFAULT=0`,
qemu-guest-agent, and Wicked; grubby and NetworkManager are absent. Native helper
incompatibility is source/package evidence, not a claimed failed live install.

## Components and behavior

1. Add only the Leap15.6 x86_64 remote row, using the existing exact local source
   URL and checksum. Packages: qemu-guest-agent, kdump, kexec-tools, makedumpfile,
   dracut, grub2, curl, tar, openssl, python3, coreutils and openssh. Reuse installed
   packages; record actual resolved versions rather than infer them from names.
   Official Leap repository lists kdump2.0.3, kexec-tools2.0.27 and makedumpfile1.7.4.
   Do not add drgn when the supported distro repository does not supply it.
2. Existing image download consumes the row's optional checksum. Existing helper
   staging chooses the SUSE source for the canonical install-helper destination;
   ownership/mode/relabel and all other helper copies remain unchanged. No public
   worker argument or runtime family negotiation is added.
3. SUSE helper reuses the established download/bundle/exit protocol. Validate the
   requested single module-release identity before building paths. Install modules,
   run depmod and native dracut, then publish one isolated kernel/initramfs pair and
   KDIVE generator. Preserve default boot and existing generators across repeated
   installs. Failed fetch returns75 only when curl actually ran; invalid arguments,
   malformed release/bundle, missing tools and native tool failures are permanent.
   A failed install must not schedule reboot.
4. Generate root selection from native exported GRUB_DEVICE/UUID/PARTUUID and
   disable flags, including LVM fallback. Use native boot-device preparation and
   filesystem-relative artifact paths, independently of root. Preserve the pinned
   XFS image's actual root and serial/default arguments. Requested words are data,
   including quotes, semicolons, dollar signs and backslashes. Inherited crashkernel
   is absent unless requested. Persistent GRUB_DEFAULT/saved_entry remain unchanged;
   only boot sets next_entry=kdive. Unsupported exotic filesystems are not newly
   advertised by this single-image qualification.
5. Kdump uses the native absolute-path KDUMP_KERNELVER contract. Edit only that
   active assignment, preserve unrelated settings and propagate enable/write errors.
   Status reads existing kernel sysfs outputs; boot readiness cannot be faked by
   enabled units. Non-kdump leaves kdump configuration unchanged.
6. Add SUSE REMOTE_REPRESENTATIVES and remove only its resolved image/helper blocker.
   Extend the merged guest_boot_kernel family selection for SUSE; its two existing
   callers already supply family. Preserve the Debian observer and tests.
   No automatic alternate-path probing, unchanged cell IDs/baselines and POWER owner.


## Pinned Leap offline geometry preparation

The canonical build at5f6021 failed during repository cache creation: repo2solv
reported ENOSPC, then repository/package lookups failed. Source virtual size is
879755264 bytes; XFS root total756716 KiB. Do not fix downstream package messages
by renaming packages or disabling signature/metadata checks.

Apply only to the exact canonical opensuse-leap15.6 row and approved SHA256. In a
fresh root-owned build work directory, download to a distinct source filename,
verify SHA256 before any transformation, and retain source read-only mode0444.
All other cloud rows keep existing download/customization behavior. Preserve the
previous failed work directory; never grow, overwrite or repurpose its image.

Before creating output require qcow2 format, virtual879755264 bytes, one expected
root device /dev/sda3 with XFS, and exactly these partition byte ranges:

| Partition | Start | End | Size |
|---|---:|---:|---:|
| 1 | 1048576 | 3145727 | 2097152 |
| 2 | 3145728 | 37748735 | 34603008 |
| 3 | 37748736 | 879738367 | 841989632 |

Inspect through existing native qemu-img/guestfish tools; require partition2 vfat
and retain source partition-table type, root filesystem UUID and partition UUID,
plus checksums of the first two partition contents. Fail closed on ambiguity or
unexpected geometry. Verify virt-resize machine-readable capability includes xfs;
no installation fallback. Require at least12 GiB available on the output filesystem
for the10 GiB target plus source/metadata headroom. This is a preflight bound, not
a reservation against unrelated host writes; later ENOSPC remains a propagated failure.
Require source and output distinct regular non-symlink paths within the owned
work directory, output and customization destination absent, and no backing file.

Create only a fresh `qemu-img create -f qcow2 OUTPUT 10G`, then run
`virt-resize --format qcow2 --output-format qcow2 --align-first never --expand /dev/sda3 --unknown-filesystems error SOURCE OUTPUT`.
Use existing direct libguestfs backend and bounded appliance defaults. This is
an offline copy/expand of a stopped image, not runtime overlay growth or a guest
cloud-init dependency. No generic storage helper/framework or size field is added.

Before promotion verify source checksum unchanged, output qcow2 virtual10737418240
bytes with no backing file, unchanged partition count/type and all partition starts,
unchanged partition1/2 sizes/content checksums, unchanged root filesystem/partition
UUIDs, XFS root and expanded partition3/filesystem. Require at least8 GiB available
inside the new root before package refresh; failure retains output without staging.
Promote by same-filesystem rename into the existing customization filename only
once all checks pass, then run existing package/helper/policy tasks unchanged.
No existing output is replaced. Failure/interruption retains named source/output;
a rerun with a partial output refuses with an actionable fresh-workdir requirement.
A previously staged successful image retains the existing skip behavior.

Record input/output hashes, native versions/capabilities, before/after geometry,
root identity/free space and exact candidate in proof. Actual package build and
original guest boot/default selection must pass; do not add a separate BIOS/UEFI
matrix obligation. Existing Wicked A–F and both SUSE lifecycle cells remain required.
Separate deployed network policy is unresolved and is not authorized by image growth.

## Native Wicked component and ownership

7. Add Leap-only native files beside the current guest helpers: `kdive-wicked-policy.xml`,
   `kdive-wicked-server-local.xml`, `kdive-wicked-startup.conf` and fixed-purpose
   `kdive-wicked-ssh-return-route`. Install policy under `/etc/wicked/`, the adapter
   as `/usr/local/libexec/kdive-wicked/netconfig`, generic configuration as `/etc/wicked/server-local.xml`
   and the startup drop-in on existing `wickedd-nanny.service`. Root owns these files;
   configuration is0644, executables0755. Existing return-route helper is reused.
8. The policy uses native Ethernet link-type matching and IPv4 DHCP only; it does
   not disable IPv6 or match interface names/MAC/PCI paths. ExecStartPost registers
   the generic policy with `wicked nanny addpolicy`, enumerates actual Ethernet
   links via `ip -j link` and invokes `wicked nanny enable <actual-interface>` for
   each, then replays current lease/address state. No wildcard or nanny recheck:
   recheck CLI is unimplemented and its internal policy-name mapping is unsuitable.
   Native enable schedules the actual ready worker, rearms it and lets the main
   loop select applicable generic policies. Later DEVICE_READY/UP registration
   schedules newly ready devices against the already installed generic policy.
   This is activation, not waiting for a lease inside ExecStartPost; no new service
   or timer. The CLI ignores individual D-Bus false results: preserve stderr/logs
   and require actual lease/traffic proof, not an exit0 activation claim. Enable
   existing Wicked services. Clean first boot has no saved policies/leases and both
   Ethernet devices present before registration; prove it separately from restart
   and hotplug, which must not mask a missing activation step.
9. Before installation refuse competing ifcfg Ethernet definitions, Wicked interface
   or persisted nanny policies, foreign server-local configuration and relevant
   systemd drop-ins; allow stock loopback/templates and byte-identical owned files
   only. Inspect the pinned stock configuration/source chain, including firmware
   definitions, so another source cannot silently override the owned DHCP policy.
   Disable only cloud-init networking with the existing cloud configuration format.
   Leave netconfig, native SSH key generation and unrelated cloud-init behavior intact.
10. Install the adapter directly at `/usr/local/libexec/kdive-wicked/netconfig`,
    without a second installed path or alias. Render commands with install/remove/batch;
    native basename of the complete batch command is exactly `netconfig batch`.
    The native empty batch startup probe delegates then returns success only on
    delegate success, without address reads, route mutation or replay. Causal tests
    exercise the exact native selector and probe; actual Wicked must keep batch
    enabled and invoke the adapter, not silently fall back to individual calls.
    Generic install/remove/batch delegate the exact native arguments to netconfig
    first; nonzero delegate status is logged/returned without routing mutations.
    The adapter then interprets only DHCP/IPv4 records. A Python stdlib parser
    handles native `-i/-t/-f` arguments, `modify -i IF -s wicked-dhcp-ipv4 -I FILE`
    and `remove -i IF -s wicked-dhcp-ipv4` batch records. A nonempty native batch
    ends with exactly one standalone `update` control record, not a lease event.
    Reject missing, duplicate, malformed or nonterminal update directives; preserve
    the truly empty startup probe. No eval/source/shell expansion.
    Native non-DHCP/IPv6 records retain delegation and cause no KDIVE route change.
    Validate bounded regular non-symlink native input paths, interface identity,
    lease INTERFACE/TYPE/FAMILY and IPADDR fields; ignore unrelated lease metadata.
    Duplicate required fields, malformed relevant lines, mismatched identity and
    multiple active slirp leases fail visibly before route mutations.
11. Read actual interface/address state through `ip -j`; require lease address
    membership before installing. Only valid host addresses in the slirp subnet
    qualify; no interface-name assumption. Serialize adapter routing/replay with
    a root-owned runtime lock, re-read state inside it, and coalesce batch changes
    against final current state. This prevents an old removal from clearing a new
    owner. Existing table2291 must have only the expected owned shape before reuse;
    foreign entries cause a visible refusal rather than a flush.
12. For the current owner, call the existing helper to install the source rule and
    return route. Delete only `table main default proto dhcp via <slirp-gateway>
    dev <matched-interface>` using argv, never text evaluation. DHCP protocol is
    verified in native Wicked0.6.77 source. Static defaults, other gateways/devices,
    connected routes and other tables remain unchanged. Repeated events converge.
    Remove only when current table ownership matches the departing interface and
    no replacement valid lease/address remains. If its device/route has vanished,
    reconcile only an identifiable obsolete slirp source rule after proving no
    active owner; foreign/ambiguous state is refused. Never call generic down for
    an unrelated interface merely because its lease is not slirp.
13. Startup replay uses current native lease files plus kernel address state, not
    a remembered NIC name or cached success flag. It handles empty initial state
    without inventing a lease. Native event processing handles later acquisition.
    Batch/removal/restart behavior remains a required actual validation obligation.

## Failure model

- Actors and deployments: trusted operator builds the pinned Leap15.6 x86_64
  remote guest; privileged installed helpers consume existing authenticated KDIVE
  jobs and native Wicked events. Other distro/hostile guest-root deployments are
  not added by this change.
- Invariants and assets at stake: prevent changed persistent boot default, duplicate KDIVE slots, caller-text
  execution, wrong family helper, checksum mismatch, routing based on stale or
  ambiguous lease data, unrelated route deletion and silent foreign-policy replacement.
  Offline growth must preserve source bytes, boot partitions/starts and root identity;
  refuse partial-output reuse or promotion before postconditions.
  Detect package/build capacity and tool failures, invalid uploaded release,
  dracut/GRUB/kdump errors, wrong kernel/module identity, native delegate/adapter
  failures, stale roles, failed SSH/egress, failed cleanup and unsupported native
  input/configuration shapes. Stop and retain unmet evidence under #3082.
- Accepted failure classes: existing trusted guest-root/native configuration and nontransactional
  install/routing effects can need recovery after interruption; no success/reboot
  follows a failed install. Wicked can report readiness despite updater failure;
  this is explicitly accepted, not a traffic proof. Mutable repositories mean a
  pinned source is not a package lock. Hostile guest-root peers are outside this
  existing trust boundary; no new isolation guarantee is claimed. Host free-space
  preflight is not reservation against unrelated writes; native failures retain
  partial output and require a fresh owned work directory, not automatic cleanup.
- Covered elsewhere: Debian changes (#3081), wider matrix (outside this issue)
  and native POWER (#2818) remain with their existing owners.
  Provider forwarding uses merged #3093; any missing deployed prerequisite is
  retained as blocked evidence, never repaired with an ad-hoc firewall exception.
  No new provider API, daemon, timer, security-driver or rp_filter policy is included.

## Verification

- Causal catalog/helper selection regression, checksum rejection and exact staged
  destination/mode. Existing Fedora helper tests stay green.
- Actual helper subprocess controls for install/reinstall, native generator syntax,
  unchanged default/saved selection, one entry, root UUID/PARTUUID/device/LVM and
  separate-boot handling, argument encoding, requested/inherited crashkernel,
  kdump explicit kernel path and preserved settings, boot-id/status/one-shot reboot,
  fetch75 versus deterministic failures. Controlled faults must make new tests red.
- Canonical image playbook builds/stages the new row; inspect actual package list,
  helper hashes, source checksum and final volume digest. No image injected into an
  existing guest substitutes for this build. Measured capacity bounds one image build.
- Execute both unchanged SUSE remote deep-lifecycle cells (longterm, stable) on a
  separate provider, exact candidate-matched roles and verified retained kernels.
  Verify upload/install/boot, new boot ID, actual release/build ID/kernel digest,
  uploaded loop module and remote domain/volume/capacity cleanup. Missing routing
  prerequisite remains blocked; neither unit tests nor a manual hinted proof replaces it.
- Native Wicked subprocess controls use source-shaped lease/batch fixtures and fake
  only external commands; cover delegation, malformed/duplicate/mismatched fields,
  unchanged DNS/IPv6/non-DHCP behavior, route ownership, unrelated/static preservation,
  interleaved renewal/removal, empty startup and replay. Fault controls must turn red.
- Actual canonical image proof registers policy, observes both NIC leases and
  route/rule snapshots, exercises renewal/address replacement, removal/reactivation,
  native batch and daemon restart/reboot. Verify forwarded authenticated SSH and
  primary egress, not only command status. A test-owned DHCP interface/namespace may
  exercise lease replacement without changing shared provider DHCP; its addresses,
  leases and routes must be observed through native Wicked and cleaned afterward.
  Inject malformed input/delegate failure only in bounded adapter tests, and verify
  native adapter failure logging in the disposable guest; restore the task-owned
  fault before lifecycle qualification. Never interpret these negative arms as ready.
- Focused tests, lint, whole-tree types, shell/Ansible/docs/hooks; mandatory pre-push
  alone owns the full ordinary suite. Independent final/security review and remote CI.

## Migration and rollback

Rebuild the new catalog image from the candidate. No existing deployed image is
silently upgraded. Revert the row/helper selection and rebuild to remove the new
capability; preserve the distribution boot path as fallback. No database migration,
authority change, new daemon or new provider interface.
