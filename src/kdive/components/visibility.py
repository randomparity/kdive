"""Provider component visibility values."""

from __future__ import annotations

from enum import StrEnum


class Visibility(StrEnum):
    """Provider component visibility scopes."""

    PUBLIC = "public"
    PROJECT = "project"
    HOST_POLICY = "host-policy"


PUBLIC_VISIBILITY = Visibility.PUBLIC
