from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


MeasureKind = Literal["flow", "state", "change", "balance"]


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    name: str
    measure_kind: MeasureKind
    public_label: str
    genitive_label: str
    default_aggregate: str
    allowed_aggregates: tuple[str, ...]
    execution_metric: str | None = None
    canonical_articles: tuple[str, ...] = ()


_FLOW_AGGREGATES = ("sum", "avg", "min", "max")
_STATE_AGGREGATES = ("first", "last", "min", "max", "avg")


METRICS: dict[str, MetricDefinition] = {
    "balance": MetricDefinition(
        "balance", "balance", "баланс газа", "баланса газа", "sum", _FLOW_AGGREGATES
    ),
    "balance_section": MetricDefinition(
        "balance_section",
        "balance",
        "раздел баланса",
        "раздела баланса",
        "sum",
        _FLOW_AGGREGATES,
    ),
    "distribution": MetricDefinition(
        "distribution", "flow", "распределение газа", "распределения газа", "sum", _FLOW_AGGREGATES,
        canonical_articles=("Распределение",),
    ),
    "flow": MetricDefinition(
        "flow", "flow", "поток газа", "потока газа", "sum", _FLOW_AGGREGATES,
        execution_metric="distribution",
    ),
    "flow_balance": MetricDefinition(
        "flow_balance", "flow", "баланс потоков газа", "баланса потоков газа", "sum", _FLOW_AGGREGATES
    ),
    "incoming": MetricDefinition(
        "incoming", "flow", "поступление газа", "поступления газа", "sum", _FLOW_AGGREGATES,
        canonical_articles=("Поступление",),
    ),
    "supply": MetricDefinition(
        "supply", "flow", "поставки газа", "поставок газа", "sum", _FLOW_AGGREGATES,
        execution_metric="distribution",
    ),
    "export": MetricDefinition(
        "export", "flow", "экспорт газа", "экспорта газа", "sum", _FLOW_AGGREGATES
    ),
    "production": MetricDefinition(
        "production", "flow", "добыча газа", "добычи газа", "sum", _FLOW_AGGREGATES
    ),
    "processing": MetricDefinition(
        "processing", "flow", "переработка газа", "переработки газа", "sum", _FLOW_AGGREGATES
    ),
    "storage_injection": MetricDefinition(
        "storage_injection", "flow", "закачка газа в ПХГ", "закачки газа в ПХГ", "sum", _FLOW_AGGREGATES
    ),
    "storage_withdrawal": MetricDefinition(
        "storage_withdrawal", "flow", "отбор газа из ПХГ", "отбора газа из ПХГ", "sum", _FLOW_AGGREGATES
    ),
    "stock": MetricDefinition(
        "stock", "state", "запас газа", "запаса газа", "last", _STATE_AGGREGATES
    ),
    "stock_change": MetricDefinition(
        "stock_change", "change", "изменение запаса газа", "изменения запаса газа", "sum", _FLOW_AGGREGATES
    ),
    "consumption": MetricDefinition(
        "consumption", "flow", "потребление газа", "потребления газа", "sum", _FLOW_AGGREGATES,
        execution_metric="distribution",
        canonical_articles=("Собственные потребители",),
    ),
    "own_needs": MetricDefinition(
        "own_needs", "flow", "собственные нужды", "собственных нужд", "sum", _FLOW_AGGREGATES,
        execution_metric="distribution",
        # ``article_semantics.jsonl`` is authoritative for daily balances and
        # uses the abbreviated label below. Keep the long forms for other
        # balance families, but prefer the exact curated daily-balance label.
        canonical_articles=(
            "Собств. нужды и потери",
            "Собственные нужды",
            "Собственные нужды и потери",
        ),
    ),
    "losses": MetricDefinition(
        "losses", "flow", "потери газа", "потерь газа", "sum", _FLOW_AGGREGATES
    ),
    "lng_production": MetricDefinition(
        "lng_production", "flow", "производство СПГ", "производства СПГ", "sum", _FLOW_AGGREGATES
    ),
}


def metric_definition(metric: str) -> MetricDefinition:
    normalized = str(metric or "").strip().lower()
    return METRICS.get(
        normalized,
        MetricDefinition(
            normalized or "unknown",
            "flow",
            normalized or "показатель",
            normalized or "показателя",
            "sum",
            _FLOW_AGGREGATES,
        ),
    )


def execution_metric(metric: str) -> str:
    definition = metric_definition(metric)
    return definition.execution_metric or definition.name


def interpretation_capabilities() -> list[str]:
    return [
        "show",
        "aggregate",
        "compare",
        "compare_periods",
        "group",
        "rank",
        "calculate",
        "formula:delta",
        "formula:ratio",
        "formula:percent_of",
        "formula:percent_change",
        *(f"metric:{name}" for name in sorted(METRICS)),
        "grain:day",
        "grain:month",
        "grain:quarter",
        "grain:year",
        "geo_group",
    ]
