from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from pydantic import Field

from .contracts import CanonicalEntityRef, ContractModel


class GroupingError(ValueError):
    pass


class GroupMemberFact(ContractModel):
    entity: CanonicalEntityRef
    value: Decimal
    unit: str
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class CanonicalGroupedFact(ContractModel):
    entity_id: str
    canonical_name: str
    entity_type: str
    value: Decimal
    unit: str
    source_fact_count: int = Field(ge=1)
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class CanonicalGroupAggregator:
    """Aggregate only by canonical ID; labels never define identity."""

    def aggregate(
        self, facts: Sequence[GroupMemberFact]
    ) -> list[CanonicalGroupedFact]:
        buckets: dict[str, CanonicalGroupedFact] = {}
        for fact in facts:
            key = fact.entity.entity_id
            existing = buckets.get(key)
            if existing is None:
                buckets[key] = CanonicalGroupedFact(
                    entity_id=key,
                    canonical_name=_official_name(fact.entity.display_name),
                    entity_type=fact.entity.entity_type,
                    value=fact.value,
                    unit=fact.unit,
                    source_fact_count=1,
                    provenance=list(fact.provenance),
                )
                continue
            if existing.unit != fact.unit:
                raise GroupingError(
                    f"unit mismatch for canonical entity {key}: "
                    f"{existing.unit!r} != {fact.unit!r}"
                )
            if existing.entity_type != fact.entity.entity_type:
                raise GroupingError(f"entity type mismatch for canonical entity {key}")
            existing.value += fact.value
            existing.source_fact_count += 1
            existing.provenance.extend(fact.provenance)
        return sorted(
            buckets.values(),
            key=lambda item: (item.canonical_name.casefold(), item.entity_id),
        )


def _official_name(value: str) -> str:
    text = str(value).strip()
    return text[:1].upper() + text[1:] if text else text
