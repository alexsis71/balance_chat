from __future__ import annotations

import pytest

from balance_chat.hosting import is_v2_path, v2_upstream_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v2", True),
        ("/v2/", True),
        ("/v2/assets/app.js", True),
        ("/v2/api/v2/health?probe=1", True),
        ("/", False),
        ("/v20", False),
        ("/api/v2/health", False),
    ],
)
def test_v2_mount_detection_is_segment_safe(path: str, expected: bool) -> None:
    assert is_v2_path(path) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v2", "/"),
        ("/v2/", "/"),
        ("/v2/assets/app.js", "/assets/app.js"),
        ("/v2/api/v2/health?probe=1", "/api/v2/health?probe=1"),
    ],
)
def test_v2_mount_is_stripped_for_upstream(path: str, expected: str) -> None:
    assert v2_upstream_path(path) == expected


def test_v2_upstream_path_rejects_other_routes() -> None:
    with pytest.raises(ValueError, match="outside the V2 mount"):
        v2_upstream_path("/api/v2/health")
