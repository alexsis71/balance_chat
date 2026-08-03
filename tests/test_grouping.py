from __future__ import annotations

from decimal import Decimal

import pytest

from balance_chat.contracts import CanonicalEntityRef
from balance_chat.grouping import (
    CanonicalGroupAggregator,
    GroupMemberFact,
    GroupingError,
    member_facts_from_rows,
)


def _fact(label: str, value: str, unit: str = "тыс. м3") -> GroupMemberFact:
    return GroupMemberFact(
        entity=CanonicalEntityRef(
            entity_id="geo:ulyanovsk",
            entity_type="geo_object",
            display_name=label,
        ),
        value=Decimal(value),
        unit=unit,
        provenance=[{"source_label": label}],
    )


def test_alias_rows_sum_by_canonical_id_and_use_official_name() -> None:
    result = CanonicalGroupAggregator().aggregate(
        [
            _fact("ульяновская обл", "1314.46"),
            _fact("ульяновская", "80906.33"),
        ]
    )

    assert len(result) == 1
    assert result[0].canonical_name == "Ульяновская обл"
    assert result[0].value == Decimal("82220.79")
    assert result[0].source_fact_count == 2
    assert len(result[0].provenance) == 2


def test_grouping_rejects_unit_mismatch() -> None:
    with pytest.raises(GroupingError, match="unit mismatch"):
        CanonicalGroupAggregator().aggregate(
            [_fact("Ульяновская область", "1"), _fact("Ульяновская", "2", "м3")]
        )


def test_pipeline_rows_are_bound_by_canonical_geo_id() -> None:
    members = member_facts_from_rows(
        [
            {"geo_id": "GEO:ul", "geo": "Ульяновская область", "fact_value": 10},
            {"geo_id": "GEO:ul", "geo": "ульяновская обл", "fact_value": 5},
        ],
        dimension="geo_group",
        default_unit="тыс. м3",
    )

    grouped = CanonicalGroupAggregator().aggregate(members)

    assert len(grouped) == 1
    assert grouped[0].entity_id == "GEO:ul"
    assert grouped[0].value == Decimal("15")
