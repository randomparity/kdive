"""Worker handler for the closed remote module-volume maintenance job."""

from kdive.reconciler.cleanup.provider_resources.module_volume_reaping import (
    remote_module_volume_reap_handler,
)

__all__ = ["remote_module_volume_reap_handler"]
