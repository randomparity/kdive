"""The remote-libvirt provider package (ADR-0076, ADR-0077).

An independent provider for a genuinely remote libvirt/QEMU host the worker tier does
not share a filesystem with, driven over a mutual-TLS ``qemu+tls://`` control
transport. Deliberately shares no provider-internal layer with ``local_libvirt``:
local-libvirt remains the default provider, remote-libvirt is operator-configured
opt-in, and the bounded libvirt-API duplication is accepted in ADR-0076.
"""
