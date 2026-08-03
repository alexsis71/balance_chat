from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

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


def member_facts_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    dimension: str,
    default_unit: str | None = None,
    canonical_resolver: (
        Callable[[Mapping[str, Any], str], CanonicalEntityRef | None] | None
    ) = None,
) -> list[GroupMemberFact]:
    """Translate grouped rows without ever using a display label as identity."""
    field_map = {
        "geo": ("geo_id", "geo", "geo_object"),
        "geo_group": ("geo_id", "geo", "geo_object"),
        "balance": ("balance_id", "balance", "balance"),
        "article": ("article_id", "article_group", "article"),
    }
    if dimension not in field_map:
        raise GroupingError(f"unsupported canonical grouping dimension: {dimension}")
    id_field, label_field, entity_type = field_map[dimension]
    output: list[GroupMemberFact] = []
    for raw in rows:
        row = dict(raw)
        entity_id = row.get(id_field)
        if entity_id is None and dimension == "article":
            article_ids = row.get("article_ids") or []
            entity_id = article_ids[0] if len(article_ids) == 1 else None
        label = str(row.get(label_field) or "").strip()
        resolved = None
        if (entity_id is None or not label) and canonical_resolver is not None:
            resolved = canonical_resolver(row, dimension)
            if resolved is not None:
                entity_id = resolved.entity_id
                label = resolved.display_name
        value = next(
            (
                row.get(key)
                for key in ("fact_value", "plan_value", "value")
                if row.get(key) is not None
            ),
            None,
        )
        unit = str(row.get("unit") or default_unit or "").strip()
        if entity_id is None or not label:
            raise GroupingError("grouped row lacks canonical identity")
        if value is None or not unit:
            raise GroupingError("grouped row lacks value or unit")
        output.append(
            GroupMemberFact(
                entity=CanonicalEntityRef(
                    entity_id=str(entity_id),
                    entity_type=(resolved.entity_type if resolved is not None else entity_type),
                    display_name=label,
                ),
                value=Decimal(str(value)),
                unit=unit,
                provenance=[row],
            )
        )
    return output


def _official_name(value: str) -> str:
    text = str(value).strip()
    return text[:1].upper() + text[1:] if text else text
