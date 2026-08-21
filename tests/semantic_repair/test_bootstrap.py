from __future__ import annotations

from balance_chat import bootstrap


def test_shadow_feature_is_default_off(monkeypatch) -> None:
    monkeypatch.delenv("SEMANTIC_REPAIR_SHADOW_ENABLED", raising=False)

    assert bootstrap._semantic_shadow({}, object()) is None


def test_enabled_but_unavailable_backend_does_not_block_bootstrap(monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_REPAIR_SHADOW_ENABLED", "true")

    runner = bootstrap._semantic_shadow({}, object())

    assert runner is not None
    assert runner.enabled is True
    assert runner.backend is None
