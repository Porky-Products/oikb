"""Tests for OikbClient.list_kb_files pagination contract (#46 / PR #45).

``list_kb_files`` is complete-or-raise: it must never return a partial
file listing as if it were complete (regression guard for #43, where an
incomplete listing silently disabled the duplicate-upload guard).
Malformed payloads raise ``ValueError``, the repo convention for bad
API responses (cf. connectors/sharepoint.py, connectors/zendesktickets.py).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from oikb.client import OikbClient

_BASE = "https://owui.example.com"
_FILES_URL = f"{_BASE}/api/v1/knowledge/kb1/files"
_OMIT_TOTAL = object()


def _file(fid: str) -> dict[str, Any]:
    return {"id": fid, "hash": f"hash-{fid}"}


def _page(items: Any, total: Any = _OMIT_TOTAL) -> httpx.Response:
    """Build one GET /knowledge/{id}/files response.

    ``total`` defaults to a sentinel so tests can distinguish an omitted
    ``total`` from an explicit ``null`` or ``0``.
    """
    payload: dict[str, Any] = {"items": items}
    if total is not _OMIT_TOTAL:
        payload["total"] = total
    return httpx.Response(200, json=payload)


def _client() -> OikbClient:
    return OikbClient(base_url=_BASE, token="token")


@respx.mock
def test_boolean_total_raises() -> None:
    # JSON `true` parses to Python True; bool is an int subclass, so the
    # old isinstance(total, int) check accepted it as a (wrong) total.
    route = respx.get(_FILES_URL).mock(side_effect=[_page([_file("f1")], total=True)])
    with _client() as client, pytest.raises(ValueError, match="total"):
        client.list_kb_files("kb1")
    assert route.call_count == 1


@respx.mock
def test_server_truncation_before_total_raises() -> None:
    # Server reports 5 files but an early page comes back empty: the
    # listing is incomplete, so it must raise rather than return 2 files
    # as if complete.
    route = respx.get(_FILES_URL).mock(
        side_effect=[
            _page([_file("f1"), _file("f2")], total=5),
            _page([], total=5),
        ]
    )
    with _client() as client, pytest.raises(ValueError, match="stalled"):
        client.list_kb_files("kb1")
    assert route.call_count == 2


@respx.mock
def test_repeated_page_before_total_raises() -> None:
    # A server that keeps serving the same page makes no progress toward
    # total; previously this returned a partial list as if complete.
    route = respx.get(_FILES_URL).mock(
        side_effect=[
            _page([_file("f1")], total=3),
            _page([_file("f1")], total=3),
        ]
    )
    with _client() as client, pytest.raises(ValueError, match="stalled"):
        client.list_kb_files("kb1")
    assert route.call_count == 2


@respx.mock
def test_page_cap_before_total_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # The safety cap must never silently return a partial listing.
    monkeypatch.setattr("oikb.client._KB_FILES_MAX_PAGES", 3)
    route = respx.get(_FILES_URL).mock(
        side_effect=[
            _page([_file("f1")], total=100),
            _page([_file("f2")], total=100),
            _page([_file("f3")], total=100),
        ]
    )
    with _client() as client, pytest.raises(ValueError, match="safety cap"):
        client.list_kb_files("kb1")
    assert route.call_count == 3


@respx.mock
def test_duplicate_ids_within_and_across_pages_deduped() -> None:
    # In-page duplicates used to inflate len(files) relative to total,
    # letting the loop stop early (or never reach total); dedup now
    # covers a single page as well as earlier pages.
    route = respx.get(_FILES_URL).mock(
        side_effect=[
            _page([_file("f1"), _file("f2"), _file("f1")], total=3),
            _page([_file("f3"), _file("f1")], total=3),
        ]
    )
    with _client() as client:
        result = client.list_kb_files("kb1")
    assert [f["id"] for f in result] == ["f1", "f2", "f3"]
    assert route.call_count == 2


@pytest.mark.parametrize("items", [{"f1": "not-a-list"}, "f1,f2", 42])
@respx.mock
def test_items_not_a_list_raises(items: Any) -> None:
    respx.get(_FILES_URL).mock(return_value=_page(items, total=1))
    with _client() as client, pytest.raises(ValueError, match="items"):
        client.list_kb_files("kb1")


@respx.mock
def test_multi_page_reaches_total_exactly() -> None:
    route = respx.get(_FILES_URL).mock(
        side_effect=[
            _page([_file("f1"), _file("f2")], total=4),
            _page([_file("f3"), _file("f4")], total=4),
        ]
    )
    with _client() as client:
        result = client.list_kb_files("kb1")
    assert [f["id"] for f in result] == ["f1", "f2", "f3", "f4"]
    assert route.call_count == 2


@respx.mock
def test_natural_exhaustion_without_total() -> None:
    # No total reported: a page yielding no new files is the legitimate
    # end of the listing, not an error.
    route = respx.get(_FILES_URL).mock(
        side_effect=[
            _page([_file("f1")]),
            _page([]),
        ]
    )
    with _client() as client:
        result = client.list_kb_files("kb1")
    assert [f["id"] for f in result] == ["f1"]
    assert route.call_count == 2


@respx.mock
def test_issue43_never_returns_partial_as_complete() -> None:
    """Regression guard for #43: list_kb_files is complete-or-raise.

    A truncated listing used to be returned as if complete, which
    silently disabled the duplicate-upload guard (the original #43
    incident).  Any server that stops serving new files before the
    reported total must raise instead.
    """
    route = respx.get(_FILES_URL).mock(
        side_effect=[
            _page([_file("f1"), _file("f2")], total=3),
            _page([_file("f2")], total=3),  # no new files; 2 of 3 collected
        ]
    )
    with _client() as client, pytest.raises(ValueError, match="stalled"):
        client.list_kb_files("kb1")
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# PR #47 Copilot review findings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [["not", "an", "object"], "a string", 123])
@respx.mock
def test_non_object_response_body_raises(body: Any) -> None:
    # A 200 whose body is not a JSON object used to coerce to {} and
    # return [] as if it were the complete listing.
    respx.get(_FILES_URL).mock(return_value=httpx.Response(200, json=body))
    with _client() as client, pytest.raises(ValueError, match="JSON object"):
        client.list_kb_files("kb1")


@respx.mock
def test_json_null_response_body_raises() -> None:
    # JSON null is not an object: it must not coerce to {} and return []
    # as if complete.
    respx.get(_FILES_URL).mock(return_value=httpx.Response(200, content=b"null"))
    with _client() as client, pytest.raises(ValueError, match="JSON object"):
        client.list_kb_files("kb1")


@pytest.mark.parametrize("total", ["100", -1, 1.5])
@respx.mock
def test_malformed_total_raises(total: Any) -> None:
    # A non-integer total used to be tolerated as absent, silently swapping
    # complete-or-raise for lenient natural exhaustion; a negative total
    # satisfied len(files) >= total on page 1 and returned a partial list
    # as complete. Both must raise.
    respx.get(_FILES_URL).mock(return_value=_page([_file("f1")], total=total))
    with _client() as client, pytest.raises(ValueError, match="total"):
        client.list_kb_files("kb1")


@respx.mock
def test_non_object_entry_raises() -> None:
    # A non-object entry used to traceback with AttributeError on f.get;
    # it is malformed data, consistent with the other listing guards.
    respx.get(_FILES_URL).mock(return_value=_page(["not-an-object"], total=1))
    with _client() as client, pytest.raises(ValueError, match="entry"):
        client.list_kb_files("kb1")
