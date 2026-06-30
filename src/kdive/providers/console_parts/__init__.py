"""Pure console-part rotation core (issue #892).

Slices a System's growing console into redacted, threshold-sized parts using the
seam-overlap carry mechanism proven in the remote-libvirt console collector
(ADR-0095). The rotation primitive here is pure (no I/O) so the stateless local
observation job can reproduce the collector's in-memory carry from a persisted
sidecar.
"""
