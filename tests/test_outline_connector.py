"""Tests for the Outline connector's manifest pagination guards."""

from __future__ import annotations

import httpx
import pytest
import respx

from oikb.connectors.outline import _MAX_PAGES, OutlineConnector


def _docs(start: int, count: int) -> list[dict]:
    return [
        {"id": f"doc-{i}", "title": f"Doc {i}", "text": f"Body {i}"}
        for i in range(start, start + count)
    ]


@respx.mock
def test_repeated_identical_page_raises() -> None:
    respx.post("https://outline.example/api/documents.list").mock(
        return_value=httpx.Response(200, json={"data": _docs(0, 100)})
    )
    with (
        OutlineConnector(token="token", base_url="https://outline.example") as connector,
        pytest.raises(ValueError, match="no progress"),
    ):
        connector.build_manifest()


@respx.mock
def test_already_seen_ids_are_skipped_without_error() -> None:
    respx.post("https://outline.example/api/documents.list").mock(
        side_effect=[
            httpx.Response(200, json={"data": _docs(0, 100)}),
            httpx.Response(200, json={"data": _docs(50, 100)}),  # 50 overlap, 50 new
            httpx.Response(200, json={"data": _docs(150, 10)}),  # short page ends the loop
        ]
    )
    with OutlineConnector(token="token", base_url="https://outline.example") as connector:
        manifest = connector.build_manifest()
    assert len(manifest) == 160
    assert len({entry.filename for entry in manifest}) == 160


@respx.mock
def test_page_cap_raises() -> None:
    calls = 0

    def endless_unique_page(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": _docs((calls - 1) * 100, 100)})

    respx.post("https://outline.example/api/documents.list").mock(side_effect=endless_unique_page)
    with (
        OutlineConnector(token="token", base_url="https://outline.example") as connector,
        pytest.raises(ValueError, match="pages without completing"),
    ):
        connector.build_manifest()
    assert calls == _MAX_PAGES + 1


@respx.mock
def test_exact_multiple_of_page_budget_completes() -> None:
    calls = 0

    def paged(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= _MAX_PAGES:
            return httpx.Response(200, json={"data": _docs((calls - 1) * 100, 100)})
        return httpx.Response(200, json={"data": []})

    respx.post("https://outline.example/api/documents.list").mock(side_effect=paged)
    with OutlineConnector(token="token", base_url="https://outline.example") as connector:
        manifest = connector.build_manifest()
    assert len(manifest) == _MAX_PAGES * 100
    assert calls == _MAX_PAGES + 1