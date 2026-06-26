"""Per-family rootfs customizers (ADR-0251).

Each :class:`~kdive.images.families.base.FamilyCustomizer` encodes how an OS family customizes a
base image into a kdive-ready rootfs (package install, kdump/sshd enable, readiness unit, image
normalization). The MVP ships :class:`~kdive.images.families.rhel.RhelFamily`.
"""
