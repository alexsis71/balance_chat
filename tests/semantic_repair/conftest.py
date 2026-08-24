from __future__ import annotations

from datetime import date

import pytest

from balance_chat.contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ContextContractV2,
    ContextScope,
    OperandEntityRef,
    Operation,
    PeriodRef,
)


@pytest.fixture
def active_state() -> ContextContractV2:
    intent = AnalysisIntent(
        operation=Operation.SHOW,
        operands=[
            AnalysisOperand(
                operand_id="flow",
                metric="distribution",
                entities=[
                    OperandEntityRef(
                        role="balance",
                        entity=CanonicalEntityRef(
                            entity_id="BAL:secret",
                            entity_type="balance",
                            display_name="ГП ТГ Томск",
                        ),
                    ),
                    OperandEntityRef(
                        role="destination",
                        entity=CanonicalEntityRef(
                            entity_id="geo:secret",
                            entity_type="geo_object",
                            display_name="Сургут",
                        ),
                    ),
                ],
            )
        ],
        periods=[PeriodRef(date_from=date(2025, 6, 1), date_to=date(2025, 7, 1))],
    )
    return ContextContractV2(
        session_id="semantic-shadow-test",
        revision=1,
        active_dialog_scope=ContextScope(intent=intent, turn_id="turn-1"),
    )
