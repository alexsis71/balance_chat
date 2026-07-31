from __future__ import annotations

import json
from pathlib import Path

from balance_chat.contracts import ContextContractV2, ContextMutation


def main() -> None:
    destination = Path(__file__).resolve().parents[1] / "schemas"
    destination.mkdir(exist_ok=True)
    for name, model in (
        ("context-contract-v2.schema.json", ContextContractV2),
        ("context-mutation-v2.schema.json", ContextMutation),
    ):
        (destination / name).write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

