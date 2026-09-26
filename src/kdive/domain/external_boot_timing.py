"""Persisted local external-boot timing contract (ADR-0684)."""

import math
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kdive.domain.errors import CategorizedError, ErrorCategory

_MAX_SECONDS = timedelta.max.days * 86_400 + timedelta.max.seconds


class LocalExternalBootTimingV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: Literal["local-external-boot-timing-v1"] = Field(
        default="local-external-boot-timing-v1", alias="schema"
    )
    accel: Literal["kvm", "tcg"] | None = None
    console_window_s: int = Field(strict=True, gt=0, le=_MAX_SECONDS)
    deadline_budget_s: int = Field(strict=True, gt=0, le=_MAX_SECONDS)

    @model_validator(mode="after")
    def _window_fits(self) -> LocalExternalBootTimingV1:
        if self.deadline_budget_s <= self.console_window_s:
            raise ValueError("deadline_budget_s must exceed console_window_s")
        return self


def resolve_local_timing(
    accel: str | None, base_window_s: int, tcg_multiplier: float
) -> LocalExternalBootTimingV1:
    """Derive the same bounded snapshot in server and authority host processes."""
    multiplier = 1.0 if accel == "kvm" else tcg_multiplier
    try:
        window = math.ceil(base_window_s * multiplier)
        budget = math.ceil((base_window_s + 300) * multiplier)
    except OverflowError, ValueError:
        window = budget = _MAX_SECONDS + 1
    if not 0 < window < budget <= _MAX_SECONDS:
        raise CategorizedError(
            "local external-boot timing exceeds the supported range; reduce the boot window",
            category=ErrorCategory.CONFIGURATION_ERROR,
            terminal=True,
        )
    return LocalExternalBootTimingV1(
        accel=accel if accel in {"kvm", "tcg"} else None,
        console_window_s=window,
        deadline_budget_s=budget,
    )


def timing_deadline(clock: datetime, seconds: int) -> datetime:
    """Refuse a calendar-overflowing absolute deadline before persistence."""
    try:
        return clock + timedelta(seconds=seconds)
    except OverflowError:
        raise CategorizedError(
            "local external-boot deadline exceeds the calendar range; reduce the boot window",
            category=ErrorCategory.CONFIGURATION_ERROR,
            terminal=True,
        ) from None
