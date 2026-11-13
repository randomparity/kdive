"""List a directory through a descriptor from its first entry (#3601).

``os.listdir(fd)`` reads from the descriptor's shared offset and rewinds only after reading.
On btrfs a directory descriptor's visible index range is fixed when it is opened and, on current
kernels (observed on Linux 7.2), refreshed on each seek, so a listing misses entries created
after the descriptor was opened or last rewound; ext4 and tmpfs return them. Seeking to 0
immediately before the read returns every entry present when the listing starts.
"""

from __future__ import annotations

import os


def list_directory_descriptor(descriptor: int) -> list[str]:
    """Names in the directory open at ``descriptor``, read from offset 0."""
    os.lseek(descriptor, 0, os.SEEK_SET)
    return os.listdir(descriptor)
