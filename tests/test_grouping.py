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
    assert result[0].canonical_name == "Ульяновская область"
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


def test_legacy_scope_row_requires_curated_canonical_resolver() -> None:
    def resolve(row, dimension):
        assert dimension == "geo_group"
        assert row["article_scope"] == "Ульяновская обл."
        return CanonicalEntityRef(
            entity_id="geo:ulyanovsk",
            entity_type="geo_object",
            display_name="Ульяновская область",
        )

    members = member_facts_from_rows(
        [{"article_scope": "Ульяновская обл.", "fact_value": 7}],
        dimension="geo_group",
        default_unit="тыс. м3",
        canonical_resolver=resolve,
    )

    assert members[0].entity.entity_id == "geo:ulyanovsk"
    assert members[0].entity.display_name == "Ульяновская область"


def test_canonical_grouped_facts_can_be_grouped_again_with_authoritative_unit() -> None:
    rows = [
        {
            "entity_id": "GEO:ulyanovsk",
            "entity_type": "geo_object",
            "canonical_name": "Ульяновская область",
            "value": "10",
            "unit": "млн м3",
        },
        {
            "entity_id": "GEO:ulyanovsk",
            "entity_type": "geo_object",
            "canonical_name": "Ульяновская область",
            "value": "5",
            "unit": "млн м3",
        },
    ]

    members = member_facts_from_rows(
        rows,
        dimension="geo_group",
        authoritative_unit="тыс. м3",
    )
    grouped = CanonicalGroupAggregator().aggregate(members)

    assert len(grouped) == 1
    assert grouped[0].canonical_name == "Ульяновская область"
    assert grouped[0].value == Decimal("15")
    assert grouped[0].unit == "тыс. м3"
