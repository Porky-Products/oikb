"""Tests for the Outline connector's manifest pagination guards."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from oikb.connectors import outline
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
def test_page_cap_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # The production default is far too large to loop in a test; patch it
    # down and verify the cap mechanics against the patched budget.
    cap = 3
    monkeypatch.setattr(outline, "_MAX_PAGES", cap)
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
    assert calls == cap + 1


def test_max_pages_default_allows_large_workspaces() -> None:
    # The cap bounds a workspace-wide listing, so it must not regress to a
    # small per-object scale: 10,000 pages x 100 documents = 1,000,000
    # documents, matching the whole-listing hard stop in
    # verify_zendesk_denylist.py.
    assert _MAX_PAGES == 10_000


@respx.mock
def test_exact_multiple_of_page_budget_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = 3
    monkeypatch.setattr(outline, "_MAX_PAGES", cap)
    calls = 0

    def paged(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= cap:
            return httpx.Response(200, json={"data": _docs((calls - 1) * 100, 100)})
        return httpx.Response(200, json={"data": []})

    respx.post("https://outline.example/api/documents.list").mock(side_effect=paged)
    with OutlineConnector(token="token", base_url="https://outline.example") as connector:
        manifest = connector.build_manifest()
    assert len(manifest) == cap * 100
    assert calls == cap + 1


@respx.mock
def test_non_empty_confirming_page_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A short non-empty page just past the budget used to be processed and
    # returned as a complete manifest; the one request beyond the budget
    # exists only to confirm completion with an EMPTY page.
    cap = 3
    monkeypatch.setattr(outline, "_MAX_PAGES", cap)
    calls = 0

    def paged(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= cap:
            return httpx.Response(200, json={"data": _docs((calls - 1) * 100, 100)})
        return httpx.Response(200, json={"data": _docs(cap * 100, 10)})

    respx.post("https://outline.example/api/documents.list").mock(side_effect=paged)
    with (
        OutlineConnector(token="token", base_url="https://outline.example") as connector,
        pytest.raises(ValueError, match="pages without completing"),
    ):
        connector.build_manifest()
    assert calls == cap + 1

@respx.mock
@pytest.mark.parametrize(
    "payload",
    [
        {"data": None},
        {"data": "oops"},
        {},
        ["not", "an", "object"],
    ],
)
def test_malformed_documents_list_payload_raises(payload) -> None:
    """PR #47: a page whose body is not an object with a list-valued data
    field must fail closed. Reading it as an empty page would return the
    partial manifest as complete and hide the remaining documents."""
    respx.post("https://outline.example/api/documents.list").mock(
        return_value=httpx.Response(200, json=payload)
    )
    with (
        OutlineConnector(token="token", base_url="https://outline.example") as connector,
        pytest.raises(ValueError, match="malformed payload"),
    ):
        connector.build_manifest()


@respx.mock
def test_unresolvable_collection_fails_before_listing_documents() -> None:
    """A configured collection that cannot be resolved must fail before
    any documents.list call: leaving collectionId unset would silently
    sync the entire workspace instead of the requested scope."""
    respx.post("https://outline.example/api/collections.list").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "col-1", "name": "Other"}]}
        )
    )
    docs_route = respx.post("https://outline.example/api/documents.list").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    with (
        OutlineConnector(
            token="token", base_url="https://outline.example", collection="Wanted"
        ) as connector,
        pytest.raises(ValueError, match="not found"),
    ):
        connector.build_manifest()
    assert docs_route.call_count == 0


@respx.mock
def test_resolved_collection_scopes_documents_list() -> None:
    """A collection resolved from any page of collections.list scopes
    every documents.list request with its collectionId."""
    respx.post("https://outline.example/api/collections.list").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "col-9", "name": "Wanted"}]}
        )
    )
    captured: dict = {}

    def docs_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": _docs(0, 1)})

    docs_route = respx.post(
        "https://outline.example/api/documents.list"
    ).mock(side_effect=docs_handler)
    with OutlineConnector(
        token="token", base_url="https://outline.example", collection="Wanted"
    ) as connector:
        manifest = connector.build_manifest()
    assert [entry.filename for entry in manifest] == ["doc-0.md"]
    assert docs_route.call_count == 1
    assert captured["body"]["collectionId"] == "col-9"
