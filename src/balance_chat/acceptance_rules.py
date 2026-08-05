from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
from typing import Any, Mapping


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str


@dataclass(frozen=True)
class RuleOutcome:
    passed: bool
    actual: Any


@dataclass(frozen=True)
class RuleContext:
    current: dict[str, Any]
    history: Mapping[str, dict[str, Any]]
    audit: dict[str, Any]


_RULE_IDS: dict[str, str] = {
    # BC-01
    "T2 сохраняет metric и GEO из T1": "inherit_metric_geo_t1",
    'T4 сохраняет Самарскую область и выбирает rank "max" с grain "day"': "rank_max_keep_samara",
    'T5 сохраняет тот же operand и период, заменяя rank на "min"': "same_operand_period_min",
    'T6 меняет только GEO на "Казань" и сохраняет летний период 2025': "replace_geo_kazan_keep_summer",
    "T7 не подменяет ни один operand общим балансом газа по России": "no_general_balance_substitution",
    # BC-02
    "оба operand сохраняют исходные balance, article, direction и период T1": "compare_extrema_preserve_route_t1",
    "T3 переставляет source и destination": "reverse_route_t1",
    "T3 удаляет direction-bound article T1 и разрешает статью обратной связи заново": "reverse_article_reresolved",
    "T4 использует canonical business entities, а не одноимённые GEO": "business_roles_not_geo",
    "T6 суммирует поток внутри каждого месяца и ранжирует месячные суммы": "monthly_flow_rank",
    "T6 возвращает один победивший месяц, а не двенадцать или три строки": "one_ranked_month",
    "T7 ссылается на canonical route T1, а не разворачивает текущий route повторно": "restore_route_t1",
    # BC-03
    "T2 выполняет sum по дням внутри месяца и max по месячным корзинам": "monthly_incoming_rank_max",
    "T2 возвращает canonical месяц и месячную сумму одной строкой": "one_ranked_month",
    "T3 меняет только rank max на min": "rank_min_same_scope",
    "T5 сохраняет source/destination и суммирует тот же месяц": "sum_winner_month_same_route",
    "T6 имеет два соседних canonical месяца одинаковой длины календарных границ": "adjacent_month_compare",
    "T6 сохраняет metric incoming и обе business роли": "incoming_business_roles_preserved",
    # BC-04
    'первое упоминание "ТГ Нижний Новгород" имеет роль source business balance': "homonym_source_business",
    'второе упоминание "Нижний Новгород" имеет роль destination GEO': "homonym_destination_geo",
    "T1 не выбирает случайную статью вроде дочернего технологического объекта": "no_random_article",
    "T2 сохраняет business source и меняет только destination GEO": "keep_source_replace_destination",
    "T3 меняет source business и сохраняет destination GEO и период": "replace_source_keep_destination_period",
    "T4 трактует квалифицированное упоминание как business destination": "qualified_destination_business",
    "T6 разворачивает выбранный operand T3, а не произвольный активный operand": "reverse_selected_t3",
    # BC-05
    'T1 разрешает curated GEO group "регионы"': "geo_group_regions",
    "Москва, Краснодарский край и Ростовская область считаются равноправными членами группы": "regions_include_three",
    "T2 группирует по canonical GEO ID без повторного DB запроса, если bounded result T1 достаточен": "group_by_canonical_reuse",
    'aliases "Ульяновская", "Ульяновская обл." и "Ульяновская область" дают одну группу': "ulyanovsk_alias_single",
    "публичное имя группы равно официальному canonical display name": "official_group_names",
    'T3 использует curated group "московский регион"': "geo_group_moscow_region",
    "T3 включает Москву и Московскую область, но не выдумывает третий GEO object": "moscow_region_members",
    'T4 разрешает "Подмосковье" как alias Московской области': "podmoskovye_alias",
    "T5 сравнивает Москву и Московскую область за апрель 2025": "compare_moscow_oblast_april",
    'T7 возвращается к geo_group "регионы" и не использует stale двухэлементный scope T5': "restore_regions_group",
    # BC-10
    "T1 возвращает последнее состояние каждого месяца, а не сумму дневных запасов": "stock_month_end_last",
    "T2 ранжирует month-end stock snapshots и возвращает один месяц": "rank_month_end_min",
    "T3 меняет rank на max, сохраняя target и год": "rank_max_same_scope_year",
    "T5 вычисляет stock_at_max_date - stock_at_min_date": "stock_delta_formula",
    "T6 меняет balance во всех зависимых шагах и сохраняет 2025 год": "replace_balance_kazan_all",
    # BC-11
    'T1 использует export balance и GEO country "Китай"': "export_china_scope",
    "T3 сравнивает январь и февраль для одного canonical country": "compare_months_same_country",
    "T4 возвращает конкретную февральскую дату": "february_extremum",
    "T5 меняет country, сохраняя февраль 2025 и metric export": "replace_country_turkey_keep_scope",
    "T7 трактует Германию как GEO country, а не произвольную одноимённую article без GEO semantics": "germany_geo_not_article",
    # BC-12
    "T2 сохраняет balance и дату, выбирая section/article policy distribution": "keep_balance_date_distribution",
    "T3 сохраняет balance и дату, меняя viewpoint на incoming": "keep_balance_date_incoming",
    "T5 ссылается на balance-level scope T1 и не оставляет stale article T3": "restore_balance_level_t1",
    # BC-13
    "T2 имеет тот же fingerprint F и тот же result fingerprint": "same_semantic_result_fingerprint_t1",
    "T3 имеет тот же fingerprint F и не создаёт новый entity handle": "same_semantic_entity_handle_t1",
    "T4 имеет тот же fingerprint F": "same_semantic_fingerprint_t1",
    "T5 сохраняет semantic target F и меняет только период на июль 2025": "same_target_july",
    'T6 исправляет вероятную опечатку "апреде" в "апреле" через bounded interpretation': "typo_april_normalized",
    "T6 явно меняет scope на geo_group регионы и апрель 2025": "group_regions_april",
    'T7 связывает "их" с rows T6 и не возвращается к Самаре из T5': "group_rows_t6_not_samara",
    # BC-14
    "последний успешный scope остаётся T1": "last_successful_t1",
    "T3 использует GEO Ярославль и период T1, а не неуспешный target T2": "recover_t1_after_failure",
    "T5 продолжает pending clarification с тем же session_id и новой revision": "clarification_continuation",
    "T5 сравнивает весну и лето для Ярославля": "compare_yaroslavl_seasons",
    # BC-15
    "T1 сохраняет operand Ярославль": "operand_label_yaroslavl",
    "T2 сохраняет operand Казань и летний период": "operand_label_kazan_summer",
    "T3 сохраняет operand Самара и летний период": "operand_label_samara_summer",
    "T4 выбирает handles T1 и T2 в указанном пользователем порядке": "compare_order_t1_t2",
    "T5 либо явно применяет max к обоим operand T4, либо задаёт уточнение о target": "rank_both_or_clarify",
    "T5 не выбирает Самару только из-за recency": "not_recency_samara",
    "T6 использует result-derived winner T5 и entity handle T3": "winner_vs_samara",
    # BC-18
    "T6 использует июнь 2025 и GEO Казань": "june_kazan",
    'T7 использует canonical period handle T1 и не разбирает слово "май" заново': "restore_may_handle_t1",
}


def catalog_rule(text: str) -> RuleSpec | None:
    rule_id = _RULE_IDS.get(text)
    return RuleSpec(rule_id) if rule_id else None


def catalog_rule_count() -> int:
    return len(_RULE_IDS)


def evaluate_catalog_rule(rule_id: str, context: RuleContext) -> RuleOutcome:
    current = View(context.current)
    previous = lambda turn: View((context.history.get(turn) or {}).get("response") or {})
    audit = context.audit

    if rule_id == "inherit_metric_geo_t1":
        source = previous("T1")
        return outcome(
            current.metrics == source.metrics and current.entity_ids("geo") == source.entity_ids("geo"),
            {"metrics": current.metrics, "geo": current.entity_labels("geo")},
        )
    if rule_id == "same_operand_period_min":
        source = previous("T4")
        return outcome(
            current.scope_signature() == source.scope_signature()
            and current.ranking.get("direction") == "min",
            current.compact(),
        )
    if rule_id == "rank_max_keep_samara":
        return outcome(
            current.operation == "rank"
            and current.ranking.get("direction") == "max"
            and current.ranking.get("grain") == "day"
            and current.has_entity("Самарская область", "geo"),
            current.compact(),
        )
    if rule_id == "replace_geo_kazan_keep_summer":
        return outcome(
            current.has_entity("Казань", "geo")
            and current.has_period("2025-06-01", "2025-09-01"),
            current.compact(),
        )
    if rule_id == "no_general_balance_substitution":
        balances = current.entity_labels("balance")
        return outcome(
            len(current.operands) == 2
            and not any("баланс газа по россии" in _normalized(item) for item in balances),
            {"operand_count": len(current.operands), "balances": balances},
        )
    if rule_id == "compare_extrema_preserve_route_t1":
        source = previous("T1")
        source_scope = source.operand_scope(0)
        return outcome(
            len(current.operands) == 2
            and all(current.operand_scope(index) == source_scope for index in range(2))
            and set(current.aggregates) == {"max", "min"},
            current.compact(),
        )
    if rule_id == "reverse_route_t1":
        return _reverse_outcome(current, previous("T1"))
    if rule_id == "reverse_article_reresolved":
        source = previous("T1")
        return outcome(
            _is_reverse(current, source)
            and bool(current.entity_ids("article"))
            and current.entity_ids("article") != source.entity_ids("article"),
            {"current": current.compact(), "source": source.compact()},
        )
    if rule_id == "business_roles_not_geo":
        routed = current.entities_by_roles({"source", "destination"})
        return outcome(
            len(routed) >= 2 and all(item[2] in {"balance", "organization"} for item in routed),
            routed,
        )
    if rule_id in {"monthly_flow_rank", "monthly_incoming_rank_max"}:
        ranking = current.ranking
        return outcome(
            current.operation == "rank"
            and ranking.get("grain") == "month"
            and ranking.get("bucket_aggregate") == "sum"
            and ranking.get("direction") == "max",
            ranking,
        )
    if rule_id == "one_ranked_month":
        selected = (current.result.get("ranking") or {}).get("selected") or []
        return outcome(
            len(selected) == 1 and _period_month(selected[0]) is not None,
            {"selected_count": len(selected), "facts": len(current.facts)},
        )
    if rule_id == "restore_route_t1":
        return outcome(
            current.route_signature() == previous("T1").route_signature(),
            {"current": current.route_signature(), "T1": previous("T1").route_signature()},
        )
    if rule_id == "rank_min_same_scope":
        source = previous("T2")
        return outcome(
            current.ranking.get("direction") == "min"
            and current.scope_signature() == source.scope_signature()
            and current.periods == source.periods,
            current.compact(),
        )
    if rule_id == "sum_winner_month_same_route":
        winner = _winner_period(previous("T3"))
        return outcome(
            current.business_route_signature() == previous("T3").business_route_signature()
            and current.aggregates == ["sum"]
            and (winner is None or winner in current.periods),
            {"winner": winner, "current": current.compact()},
        )
    if rule_id == "adjacent_month_compare":
        periods = current.periods
        return outcome(
            len(periods) == 2 and periods[0][1] == periods[1][0]
            and all(_is_full_month(item) for item in periods),
            periods,
        )
    if rule_id == "incoming_business_roles_preserved":
        return outcome(
            current.metrics == ["incoming"]
            and all(current.business_route_signature())
            and current.business_route_signature() == previous("T5").business_route_signature(),
            current.compact(),
        )
    if rule_id == "homonym_source_business":
        values = [item for item in current.entities() if item[2] in {"balance", "organization"} and _labels_match("ТГ Нижний Новгород", item[1])]
        return outcome(bool(values), values)
    if rule_id == "homonym_destination_geo":
        return _entity_rule(current, "Нижний Новгород", "destination", {"geo_object"})
    if rule_id == "no_random_article":
        articles = current.entities_by_roles({"article"})
        labels = [item[1] for item in articles]
        return outcome(
            len(articles) <= 1 and not any(_normalized(label) in {"нижний новгород", "тг нижний новгород"} for label in labels),
            labels,
        )
    if rule_id == "keep_source_replace_destination":
        source = previous("T1")
        return outcome(
            current.entity_ids("source") == source.entity_ids("source")
            and current.entity_ids("destination") != source.entity_ids("destination"),
            current.compact(),
        )
    if rule_id == "replace_source_keep_destination_period":
        source = previous("T2")
        return outcome(
            current.entity_ids("source") != source.entity_ids("source")
            and current.entity_ids("destination") == source.entity_ids("destination")
            and current.periods == source.periods,
            current.compact(),
        )
    if rule_id == "qualified_destination_business":
        entities = [item for item in current.entities() if item[2] == "balance" and _labels_match("ГП ТГ Н. Новгород", item[1])]
        return outcome(bool(entities), entities)
    if rule_id == "reverse_selected_t3":
        return _reverse_outcome(current, previous("T3"))
    if rule_id == "geo_group_regions":
        return outcome(
            current.has_group("регион")
            or (
                any(item.get("dimension") == "geo" for item in current.grouping)
                and len(current.facts) >= 3
            ),
            current.compact(),
        )
    if rule_id == "regions_include_three":
        labels = current.result_labels()
        return outcome(
            all(_contains_label(labels, item) for item in ("Москва", "Краснодарский край", "Ростовская область")),
            labels,
        )
    if rule_id == "group_by_canonical_reuse":
        execution = (audit.get("turn_completed") or {}).get("execution") or {}
        return outcome(
            current.operation == "group"
            and bool(current.grouping)
            and (execution.get("grouping_source") == "result_reference" or execution.get("source_execution_count") == 0),
            {"grouping": current.grouping, "execution": execution},
        )
    if rule_id == "ulyanovsk_alias_single":
        matches = [label for label in current.result_labels() if "ульяновск" in _normalized(label)]
        return outcome(len(matches) == 1, matches)
    if rule_id == "official_group_names":
        labels = current.result_labels()
        abbreviated = [label for label in labels if re.search(r"\bобл\.?\b", label, re.IGNORECASE)]
        return outcome(not abbreviated, {"abbreviated": abbreviated, "sample": labels[:10]})
    if rule_id == "geo_group_moscow_region":
        return outcome(current.has_group("московск") and current.has_group("регион"), current.compact())
    if rule_id == "moscow_region_members":
        labels = [label for label in current.result_labels() if "моск" in _normalized(label)]
        return outcome(
            len(labels) == 2
            and _contains_label(labels, "Москва")
            and _contains_label(labels, "Московская область"),
            labels,
        )
    if rule_id == "podmoskovye_alias":
        return outcome(current.has_entity("Московская область", "geo"), current.compact())
    if rule_id == "compare_moscow_oblast_april":
        labels = current.operand_labels()
        return outcome(
            current.operation == "compare"
            and len(current.operands) == 2
            and _contains_label(labels, "Москва")
            and _contains_label(labels, "Московская область")
            and current.has_period("2025-04-01", "2025-05-01"),
            current.compact(),
        )
    if rule_id == "restore_regions_group":
        return outcome(current.has_group("регион") and len(current.operands) != 2, current.compact())
    if rule_id == "stock_month_end_last":
        return outcome(
            current.metrics == ["stock"] and current.grain == "month"
            and current.aggregates == ["last"] and len(current.facts) == 12,
            current.compact(),
        )
    if rule_id == "rank_month_end_min":
        return outcome(
            current.metrics == ["stock"] and current.ranking.get("direction") == "min"
            and current.ranking.get("grain") == "month"
            and current.ranking.get("bucket_aggregate") == "last"
            and len((current.result.get("ranking") or {}).get("selected") or []) == 1,
            current.compact(),
        )
    if rule_id == "rank_max_same_scope_year":
        source = previous("T2")
        return outcome(
            current.ranking.get("direction") == "max"
            and current.scope_signature() == source.scope_signature()
            and current.periods == source.periods,
            current.compact(),
        )
    if rule_id == "stock_delta_formula":
        return outcome(
            current.operation == "calculate" and current.formula.get("operator") == "delta"
            and len(current.operands) == 2 and all(metric == "stock" for metric in current.metrics),
            current.compact(),
        )
    if rule_id == "replace_balance_kazan_all":
        balances = current.entities_by_roles({"balance"})
        return outcome(
            bool(balances) and all("казан" in _normalized(item[1]) for item in balances)
            and current.has_period("2025-01-01", "2026-01-01"),
            current.compact(),
        )
    if rule_id == "export_china_scope":
        return outcome(
            current.metrics == ["export"] and current.has_entity("Китай", "geo")
            and bool(current.entity_ids("balance")),
            current.compact(),
        )
    if rule_id == "compare_months_same_country":
        return outcome(
            current.operation == "compare_periods" and len(current.operands) == 1
            and current.has_period("2025-01-01", "2025-02-01")
            and current.has_period("2025-02-01", "2025-03-01")
            and current.has_entity("Китай", "geo"),
            current.compact(),
        )
    if rule_id == "february_extremum":
        values = current.extremum_dates()
        return outcome(bool(values) and all(value.startswith("2025-02-") for value in values), values)
    if rule_id == "replace_country_turkey_keep_scope":
        return outcome(
            current.metrics == ["export"] and current.has_entity("Турция", "geo")
            and current.has_period("2025-02-01", "2025-03-01"),
            current.compact(),
        )
    if rule_id == "germany_geo_not_article":
        geo = current.find_entities("Германия", "geo")
        article = current.find_entities("Германия", "article")
        return outcome(bool(geo) and not article, {"geo": geo, "article": article})
    if rule_id == "keep_balance_date_distribution":
        return _keep_balance_period_metric(current, previous("T1"), "distribution")
    if rule_id == "keep_balance_date_incoming":
        return _keep_balance_period_metric(current, previous("T2"), "incoming")
    if rule_id == "restore_balance_level_t1":
        source = previous("T1")
        return outcome(
            current.entity_ids("balance") == source.entity_ids("balance")
            and current.periods == source.periods and not current.entity_ids("article"),
            current.compact(),
        )
    if rule_id in {"same_semantic_fingerprint_t1", "same_semantic_result_fingerprint_t1"}:
        source_data = context.history.get("T1") or {}
        semantic_equal = current.semantic_fingerprint() == previous("T1").semantic_fingerprint()
        current_result = (audit.get("turn_completed") or {}).get("result_fingerprint")
        source_result = ((source_data.get("audit") or {}).get("turn_completed") or {}).get("result_fingerprint")
        result_equal = bool(current_result and source_result and current_result == source_result)
        passed = semantic_equal and (result_equal if rule_id.endswith("result_fingerprint_t1") else True)
        return outcome(passed, {"semantic_equal": semantic_equal, "result_equal": result_equal})
    if rule_id == "same_semantic_entity_handle_t1":
        source_data = context.history.get("T1") or {}
        current_handles = _entity_handles(audit)
        source_handles = _entity_handles(source_data.get("audit") or {})
        return outcome(
            current.semantic_fingerprint() == previous("T1").semantic_fingerprint()
            and bool(current_handles) and current_handles == source_handles,
            {
                "current": sorted(current_handles),
                "T1": sorted(source_handles),
            },
        )
    if rule_id == "same_target_july":
        return outcome(
            current.target_fingerprint() == previous("T1").target_fingerprint()
            and current.has_period("2025-07-01", "2025-08-01"),
            current.compact(),
        )
    if rule_id == "typo_april_normalized":
        normalized = str((audit.get("turn_completed") or {}).get("normalized_message") or "")
        value = _normalized(normalized)
        return outcome("апрел" in value and "апред" not in value, normalized)
    if rule_id == "group_regions_april":
        return outcome(current.has_group("регион") and current.has_period("2025-04-01", "2025-05-01"), current.compact())
    if rule_id == "group_rows_t6_not_samara":
        execution = (audit.get("turn_completed") or {}).get("execution") or {}
        return outcome(
            execution.get("grouping_source") == "result_reference"
            and not current.has_entity("Самара", "geo"),
            {"execution": execution, "entities": current.operand_labels()},
        )
    if rule_id == "last_successful_t1":
        source_turn_id = previous("T1").active_turn_id
        return outcome(current.last_successful_turn_id == source_turn_id, {
            "actual": current.last_successful_turn_id, "T1": source_turn_id
        })
    if rule_id == "recover_t1_after_failure":
        source = previous("T1")
        return outcome(
            current.entity_ids("geo") == source.entity_ids("geo") and current.periods == source.periods,
            current.compact(),
        )
    if rule_id == "clarification_continuation":
        clarification = previous("T4")
        return outcome(
            clarification.response_status == "needs_clarification"
            and current.session_id == clarification.session_id
            and current.revision == clarification.revision + 1,
            {"status": clarification.response_status, "session": current.session_id, "revision": current.revision},
        )
    if rule_id == "compare_yaroslavl_seasons":
        return outcome(
            current.operation == "compare_periods" and current.has_entity("Ярославль", "geo")
            and current.has_period("2025-03-01", "2025-06-01")
            and current.has_period("2025-06-01", "2025-09-01"),
            current.compact(),
        )
    if rule_id == "operand_label_yaroslavl":
        return outcome(current.has_label("Ярославль"), current.compact())
    if rule_id == "operand_label_kazan_summer":
        return outcome(current.has_label("Казань") and current.has_period("2025-06-01", "2025-09-01"), current.compact())
    if rule_id == "operand_label_samara_summer":
        return outcome(current.has_label("Самара") and current.has_period("2025-06-01", "2025-09-01"), current.compact())
    if rule_id == "compare_order_t1_t2":
        return outcome(
            current.operation == "compare" and len(current.operands) == 2
            and current.operand_entity_ids(0) == previous("T1").operand_entity_ids(0)
            and current.operand_entity_ids(1) == previous("T2").operand_entity_ids(0),
            current.compact(),
        )
    if rule_id == "rank_both_or_clarify":
        return outcome(
            current.response_status == "needs_clarification"
            or (current.operation == "rank" and len(current.operands) == 2),
            {"status": current.response_status, "operation": current.operation, "operands": len(current.operands)},
        )
    if rule_id == "not_recency_samara":
        return outcome(
            current.response_status == "needs_clarification" or not current.has_entity("Самара", "geo"),
            current.compact(),
        )
    if rule_id == "winner_vs_samara":
        return outcome(
            current.operation == "compare" and len(current.operands) == 2
            and current.has_entity("Самара", "geo"),
            current.compact(),
        )
    if rule_id == "june_kazan":
        return outcome(current.has_entity("Казань", "geo") and current.has_period("2025-06-01", "2025-07-01"), current.compact())
    if rule_id == "restore_may_handle_t1":
        source_data = context.history.get("T1") or {}
        current_handles = _period_handles(audit)
        source_handles = _period_handles(source_data.get("audit") or {})
        return outcome(
            current.has_period("2025-05-01", "2025-06-01")
            and bool(current_handles & source_handles),
            {"current": sorted(current_handles), "T1": sorted(source_handles)},
        )
    return outcome(False, f"unknown catalog rule: {rule_id}")


class View:
    def __init__(self, response: dict[str, Any]):
        self.response = response
        context = response.get("context") or {}
        active = context.get("active") or {}
        self.intent = active.get("intent") or {}
        self.result = response.get("result") or {}
        self.operation = str(self.intent.get("operation") or self.result.get("operation") or "")
        self.operands = list(self.intent.get("operands") or [])
        self.metrics = [str(item.get("metric") or "") for item in self.operands]
        self.aggregates = [str(item.get("aggregate_type") or "") for item in self.operands]
        self.periods = _periods(self.intent)
        self.grain = self.intent.get("grain")
        self.grouping = list(self.intent.get("grouping") or [])
        self.ranking = self.intent.get("ranking") or {}
        self.formula = self.intent.get("formula") or {}
        self.facts = list(self.result.get("facts") or [])
        self.response_status = str(response.get("status") or "")
        self.session_id = str((response.get("session") or {}).get("session_id") or "")
        self.revision = int((response.get("session") or {}).get("revision") or 0)
        self.active_turn_id = str(active.get("turn_id") or "")
        self.last_successful_turn_id = str(((context.get("last_successful") or {}).get("turn_id")) or "")

    def entities(self) -> list[tuple[str, str, str, str]]:
        values = []
        for operand in self.operands:
            for item in operand.get("entities") or []:
                nested = item.get("entity") if isinstance(item.get("entity"), dict) else item
                values.append((
                    str(item.get("role") or ""),
                    str(nested.get("display_name") or nested.get("label") or ""),
                    str(nested.get("entity_type") or ""),
                    str(nested.get("entity_id") or ""),
                ))
        return values

    def entities_by_roles(self, roles: set[str]) -> list[tuple[str, str, str, str]]:
        return [item for item in self.entities() if item[0] in roles]

    def entity_ids(self, role: str) -> tuple[str, ...]:
        if role == "geo":
            return tuple(item[3] for item in self.entities() if item[2] in {"geo_object", "geo_group"})
        return tuple(item[3] for item in self.entities() if item[0] == role)

    def entity_labels(self, role: str) -> tuple[str, ...]:
        if role == "geo":
            return tuple(item[1] for item in self.entities() if item[2] in {"geo_object", "geo_group"})
        return tuple(item[1] for item in self.entities() if item[0] == role)

    def find_entities(self, label: str, role: str) -> list[tuple[str, str, str, str]]:
        values = self.entities()
        if role == "geo":
            values = [item for item in values if item[2] in {"geo_object", "geo_group"}]
        else:
            values = [item for item in values if item[0] == role]
        return [item for item in values if _labels_match(label, item[1])]

    def has_entity(self, label: str, role: str) -> bool:
        return bool(self.find_entities(label, role))

    def has_label(self, label: str) -> bool:
        return any(_labels_match(label, item[1]) for item in self.entities())

    def has_period(self, date_from: str, date_to: str) -> bool:
        return [date_from, date_to] in self.periods

    def has_group(self, fragment: str) -> bool:
        fragment = _normalized(fragment)
        if any(fragment in _normalized(str(item.get("canonical_group_id") or "")) for item in self.grouping):
            return True
        return any(fragment in _normalized(item[1]) for item in self.entities() if item[2] == "geo_group")

    def operand_scope(self, index: int) -> tuple[Any, ...]:
        if index >= len(self.operands):
            return ()
        operand = self.operands[index]
        entities = []
        for item in operand.get("entities") or []:
            nested = item.get("entity") if isinstance(item.get("entity"), dict) else item
            entities.append((item.get("role"), nested.get("entity_type"), nested.get("entity_id")))
        periods = tuple((item.get("date_from"), item.get("date_to")) for item in operand.get("periods") or [])
        return str(operand.get("metric") or ""), tuple(sorted(entities)), periods

    def scope_signature(self) -> tuple[Any, ...]:
        return tuple(self.operand_scope(index) for index in range(len(self.operands)))

    def route_signature(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return self.entity_ids("source"), self.entity_ids("destination")

    def business_route_signature(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        explicit = self.route_signature()
        source = explicit[0]
        destination = explicit[1]
        if self.metrics and self.metrics[0] == "incoming":
            source = source or self.entity_ids("article")
            destination = destination or self.entity_ids("balance")
        elif self.metrics and self.metrics[0] in {"distribution", "supply"}:
            source = source or self.entity_ids("balance")
            destination = destination or self.entity_ids("article")
        return source, destination

    def operand_entity_ids(self, index: int) -> tuple[str, ...]:
        if index >= len(self.operands):
            return ()
        values = []
        for item in self.operands[index].get("entities") or []:
            nested = item.get("entity") if isinstance(item.get("entity"), dict) else item
            values.append(str(nested.get("entity_id") or ""))
        return tuple(values)

    def operand_labels(self) -> list[str]:
        return [item[1] for item in self.entities()]

    def result_labels(self) -> list[str]:
        labels: list[str] = []
        candidates = [*self.facts, *(self.result.get("rows") or [])]
        ranking = self.result.get("ranking") or {}
        candidates.extend(ranking.get("selected") or [])
        for item in candidates:
            if not isinstance(item, dict):
                continue
            for key in ("label", "canonical_name", "article_name", "article_scope", "balance", "entity_name", "name"):
                value = str(item.get(key) or "").strip()
                if value and value not in labels:
                    labels.append(value)
        return labels

    def extremum_dates(self) -> list[str]:
        values = []
        for item in [*self.facts, *((self.result.get("ranking") or {}).get("selected") or [])]:
            if not isinstance(item, dict):
                continue
            value = str(item.get("extremum_at") or "")
            if value:
                values.append(value)
        return values

    def semantic_fingerprint(self) -> str:
        value = _canonical_intent(self.intent, include_periods=True)
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def target_fingerprint(self) -> str:
        value = _canonical_intent(self.intent, include_periods=False)
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def compact(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "metrics": self.metrics,
            "aggregates": self.aggregates,
            "entities": [(item[0], item[1], item[2]) for item in self.entities()],
            "periods": self.periods,
            "grain": self.grain,
            "grouping": self.grouping,
            "ranking": self.ranking,
            "formula": self.formula,
        }


def outcome(passed: bool, actual: Any) -> RuleOutcome:
    return RuleOutcome(bool(passed), actual)


def _reverse_outcome(current: View, source: View) -> RuleOutcome:
    return outcome(_is_reverse(current, source), {
        "current": current.route_signature(), "source": source.route_signature()
    })


def _is_reverse(current: View, source: View) -> bool:
    current_source, current_destination = current.route_signature()
    source_source, source_destination = source.route_signature()
    return bool(
        current_source and current_destination
        and current_source == source_destination
        and current_destination == source_source
    )


def _entity_rule(current: View, label: str, role: str, types: set[str]) -> RuleOutcome:
    values = current.find_entities(label, role)
    return outcome(bool(values) and all(item[2] in types for item in values), values)


def _keep_balance_period_metric(current: View, source: View, metric: str) -> RuleOutcome:
    return outcome(
        current.entity_ids("balance") == source.entity_ids("balance")
        and current.periods == source.periods and current.metrics == [metric],
        current.compact(),
    )


def _periods(intent: dict[str, Any]) -> list[list[str]]:
    values: list[list[str]] = []
    candidates = list(intent.get("periods") or [])
    for operand in intent.get("operands") or []:
        candidates.extend(operand.get("periods") or [])
    for item in candidates:
        pair = [str(item.get("date_from") or ""), str(item.get("date_to") or "")]
        if pair not in values:
            values.append(pair)
    return values


def _winner_period(view: View) -> list[str] | None:
    selected = ((view.result.get("ranking") or {}).get("selected") or [])
    if not selected:
        return None
    periods = selected[0].get("periods") or []
    if not periods:
        dimension = selected[0].get("dimension") or {}
        start = str(dimension.get("value") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
            parsed = date.fromisoformat(start)
            if view.ranking.get("grain") == "month":
                next_month = date(parsed.year + (parsed.month == 12), 1 if parsed.month == 12 else parsed.month + 1, 1)
                return [parsed.isoformat(), next_month.isoformat()]
        return None
    return [str(periods[0].get("date_from") or ""), str(periods[0].get("date_to") or "")]


def _period_month(item: dict[str, Any]) -> str | None:
    periods = item.get("periods") or []
    value = str((periods[0] if periods else {}).get("date_from") or (item.get("dimension") or {}).get("value") or "")
    return value[:7] if re.match(r"\d{4}-\d{2}", value) else None


def _is_full_month(period: list[str]) -> bool:
    try:
        start, end = date.fromisoformat(period[0]), date.fromisoformat(period[1])
    except (ValueError, TypeError):
        return False
    if start.day != 1 or end.day != 1:
        return False
    return (end.year * 12 + end.month) - (start.year * 12 + start.month) == 1


def _normalized(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", " ", value.lower().replace("ё", "е")).strip()


def _labels_match(expected: str, actual: str) -> bool:
    left, right = _normalized(expected), _normalized(actual)
    left = re.sub(r"\b(?:гп|тг)\b", " ", left).strip()
    right = re.sub(r"\b(?:гп|тг)\b", " ", right).strip()
    return bool(left and right and (left in right or right in left))


def _contains_label(labels: list[str], expected: str) -> bool:
    return any(_labels_match(expected, item) for item in labels)


def _canonical_intent(intent: dict[str, Any], *, include_periods: bool) -> dict[str, Any]:
    operands = []
    for operand in intent.get("operands") or []:
        entities = []
        for item in operand.get("entities") or []:
            nested = item.get("entity") if isinstance(item.get("entity"), dict) else item
            entities.append((item.get("role"), nested.get("entity_type"), nested.get("entity_id")))
        operands.append({
            "metric": operand.get("metric"),
            "aggregate_type": operand.get("aggregate_type"),
            "entities": sorted(entities),
            "periods": (
                sorted((item.get("date_from"), item.get("date_to")) for item in operand.get("periods") or [])
                if include_periods else []
            ),
        })
    return {
        "operation": intent.get("operation"),
        "operands": operands,
        "periods": (
            sorted((item.get("date_from"), item.get("date_to")) for item in intent.get("periods") or [])
            if include_periods else []
        ),
        "grouping": intent.get("grouping") or [],
        "grain": intent.get("grain"),
        "comparison": _without_operand_ids(intent.get("comparison") or {}),
        "formula": _without_operand_ids(intent.get("formula") or {}),
        "ranking": intent.get("ranking") or {},
    }


def _without_operand_ids(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item for key, item in value.items()
        if key not in {"baseline_operand_id", "target_operand_id", "numerator_operand_id", "denominator_operand_id"}
    }


def _entity_handles(audit: dict[str, Any]) -> set[tuple[str, str]]:
    committed = audit.get("conversation_turn_committed") or {}
    return {
        (str(item.get("canonical_id") or ""), str(item.get("handle") or ""))
        for item in committed.get("entity_tags") or []
    }


def _period_handles(audit: dict[str, Any]) -> set[str]:
    committed = audit.get("conversation_turn_committed") or {}
    return {str(item.get("handle") or "") for item in committed.get("period_tags") or [] if item.get("handle")}
