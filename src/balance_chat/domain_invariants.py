"""Non-negotiable data invariants of the AI Balances domain."""

from __future__ import annotations

from typing import Any


CANONICAL_VOLUME_UNIT = "тыс. м3"
FORBIDDEN_PLAN_FIELDS = frozenset({"plan", "plan_value"})


def canonical_volume_unit(value: Any) -> str | None:
    """Normalize any explicitly supplied volume unit to the only stored unit."""

    if value is None or not str(value).strip():
        return None
    return CANONICAL_VOLUME_UNIT
