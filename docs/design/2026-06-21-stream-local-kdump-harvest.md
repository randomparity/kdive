# Historical local kdump harvest observation

Recorded on 2026-06-21 for #657, companion to #654. This preserves a measurement from the
then-current implementation; it is not an operating procedure or a current memory/size limit.

The #115 live verification harvested an approximately 805 MB core successfully through
libguestfs `read_file`. That whole-file read placed the core in worker memory and motivated
the streaming follow-up. The original note recorded no peak-RAM measurement or exact byte
count, so the observation establishes a successful harvest at that approximate size only.

The decision and streaming rationale survive in
[ADR-0203](../adr/0203-local-libvirt-kdump-overlay-harvest.md). For current capture methods,
prerequisites, and result verification, use the [postmortem guide](../guide/toolsets/postmortem.md).
