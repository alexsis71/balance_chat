from __future__ import annotations

from ..contracts import ContextContractV2
from .geo import GeoTransitionServices, detect_geo_transition
from .period import detect_period_transition
from .types import TransitionDecision


def detect_deterministic_transition(
    *,
    message: str,
    state: ContextContractV2,
    turn_id: str,
    clarification_provided: bool,
    geo_services: GeoTransitionServices,
) -> TransitionDecision:
    """Select one already-supported deterministic PATCH or return no-match."""
    if clarification_provided or state.pending_clarification is not None:
        return TransitionDecision.no_match()
    period = detect_period_transition(state, message, turn_id)
    if period is not None:
        return period
    geo = detect_geo_transition(state, message, turn_id, geo_services)
    return geo if geo is not None else TransitionDecision.no_match()
