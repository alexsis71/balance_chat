from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
from types import MappingProxyType

from ..contracts import (
    AnalysisIntent,
    ContextContractV2,
    ContextMutation,
    FieldMutation,
    IntentPatch,
    MutationAction,
    Operation,
    PeriodRef,
)
from .types import TransitionDecision, TransitionKind


MONTH_PATTERNS = (
    (r"\bянвар\w*", 1), (r"\bфеврал\w*", 2), (r"\bмарт\w*", 3),
    (r"\bапрел\w*", 4), (r"\bма(?:й|я|е|ю|ем)\b", 5),
    (r"\bиюн\w*", 6), (r"\bиюл\w*", 7), (r"\bавгуст\w*", 8),
    (r"\bсентябр\w*", 9), (r"\bоктябр\w*", 10),
    (r"\bноябр\w*", 11), (r"\bдекабр\w*", 12),
)

_RUSSIAN_MONTHS = {
    r"январ\w*": 1, r"феврал\w*": 2, r"март\w*": 3,
    r"апрел\w*": 4, r"ма(?:й|я|е)": 5, r"июн\w*": 6,
    r"июл\w*": 7, r"август\w*": 8, r"сентябр\w*": 9,
    r"октябр\w*": 10, r"ноябр\w*": 11, r"декабр\w*": 12,
}


@dataclass(frozen=True, slots=True)
class PeriodPatchCandidate:
    periods: tuple[PeriodRef, ...]


def _normalize_text(value: str) -> str:
    text = str(value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", text).split())


def explicit_single_day_period(message: str) -> PeriodRef | None:
    """Parse one explicit calendar day using bounded generic date forms."""
    text = str(message)
    candidates: list[date] = []
    occupied: list[tuple[int, int]] = []
    for match in re.finditer(r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)", text):
        try:
            candidates.append(
                date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            )
            occupied.append(match.span())
        except ValueError:
            return None
    for match in re.finditer(
        r"(?<!\d)([0-3]?\d)[./-]([01]?\d)[./-](20\d{2})(?!\d)", text
    ):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        try:
            candidates.append(
                date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
            )
        except ValueError:
            return None
    normalized = _normalize_text(text)
    for month_pattern, month in _RUSSIAN_MONTHS.items():
        match = re.search(
            rf"\b([0-3]?\d)\s+{month_pattern}\s+(20\d{{2}})\b", normalized
        )
        if not match:
            continue
        try:
            candidates.append(date(int(match.group(2)), month, int(match.group(1))))
        except ValueError:
            return None
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        return None
    start = unique[0]
    return PeriodRef(date_from=start, date_to=start + timedelta(days=1))


def detect_period_followup(
    message: str,
    active_intent: AnalysisIntent,
) -> PeriodPatchCandidate | None:
    if (
        active_intent.operation not in {Operation.SHOW, Operation.AGGREGATE}
        or len(active_intent.operands) != 1
        or len(active_intent.periods) != 1
        or any(operand.periods for operand in active_intent.operands)
    ):
        return None
    followup = re.fullmatch(
        r"(?:а\s+)?(?:покажи\s+)?за\s+(?P<period>.+)",
        _normalize_text(message),
    )
    if followup is None:
        return None
    period_text = followup.group("period")
    for month_pattern, month in MONTH_PATTERNS:
        explicit_day = re.fullmatch(
            rf"[0-3]?\d\s+{month_pattern}\s+20\d{{2}}(?:\s+г(?:од(?:а)?)?)?",
            period_text,
        )
        if explicit_day is not None:
            period = explicit_single_day_period(period_text)
            return PeriodPatchCandidate((period,)) if period is not None else None
        named_month = re.fullmatch(
            rf"{month_pattern}(?:\s+(20\d{{2}})(?:\s+г(?:од(?:а)?)?)?)?",
            period_text,
        )
        if named_month is None:
            continue
        year = (
            int(named_month.group(1))
            if named_month.group(1)
            else active_intent.periods[0].date_from.year
        )
        date_from = date(year, month, 1)
        date_to = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        return PeriodPatchCandidate((PeriodRef(date_from=date_from, date_to=date_to),))
    return None


def detect_period_transition(
    state: ContextContractV2,
    message: str,
    turn_id: str,
) -> TransitionDecision | None:
    scope = state.active_dialog_scope
    if scope is None:
        return None
    candidate = detect_period_followup(message, scope.intent)
    if candidate is None:
        return None
    mutation = ContextMutation(
        turn_id=turn_id,
        user_message=message,
        normalized_message=message,
        patch=IntentPatch(
            periods=FieldMutation(
                action=MutationAction.SET,
                value=list(candidate.periods),
            )
        ),
    )
    return TransitionDecision(
        kind=TransitionKind.PATCH,
        mutation=mutation,
        interpretation_mode="deterministic_period_patch",
        diagnostic_event="deterministic_period_patch_recognized",
        diagnostic_fields=MappingProxyType({"mutation_mode": "period_patch"}),
    )
