from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from ..contracts import ContextMutation


class TransitionKind(StrEnum):
    PATCH = "patch"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    kind: TransitionKind
    mutation: ContextMutation | None = None
    interpretation_mode: str | None = None
    evidence_businesses: tuple[Any, ...] = ()
    evidence_geos: tuple[Any, ...] = ()
    diagnostic_event: str | None = None
    diagnostic_fields: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @classmethod
    def no_match(cls) -> "TransitionDecision":
        return cls(kind=TransitionKind.NO_MATCH)
