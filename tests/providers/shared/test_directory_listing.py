"""Descriptor listings start from offset 0 (#3601)."""

from __future__ import annotations

import os
from pathlib import Path

from kdive.providers.shared.directory_listing import list_directory_descriptor


def test_lists_every_entry_after_the_shared_offset_moved(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).touch()
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # A started scandir reads the whole small directory through a duplicate that shares
        # the offset, leaving it at the end: a bare os.listdir(descriptor) now returns [].
        with os.scandir(descriptor) as entries:
            next(entries)
            assert sorted(list_directory_descriptor(descriptor)) == ["a", "b", "c"]
    finally:
        os.close(descriptor)
