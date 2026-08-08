from pathlib import Path
import shutil
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from oikb.cli import _resolve_connector
from oikb.connectors.zendesktickets import ZendeskTicketsConnector, parse_zendesktickets_source


TEST_STATE_ROOT = ROOT / ".test-state" / "zendesktickets"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeHTTPClient:
    def __init__(self, pages, comments):
        self._pages = list(pages)
        self._comments = comments
        self.calls = []

    def get(self, path, params=None):
        self.calls.append({"path": path, "params": params})
        if path == "/tickets.json":
            return FakeResponse(self._pages.pop(0))
        if path.endswith("/comments.json"):
            ticket_id = int(path.split("/")[2])
            return FakeResponse({"comments": self._comments.get(ticket_id, [])})
        raise AssertionError(f"Unexpected request: {path}")


def _make_state_dir(name: str) -> Path:
    state_dir = TEST_STATE_ROOT / name
    if state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def test_parse_zendesktickets_source_requires_subdomain():
    assert parse_zendesktickets_source("zendesktickets:acme") == {"subdomain": "acme"}


def test_resolve_connector_returns_zendesktickets_connector(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    connector = _resolve_connector("zendesktickets:acme")

    assert isinstance(connector, ZendeskTicketsConnector)
    connector.close()


def test_constructor_parses_zendeskticket_env(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    connector = ZendeskTicketsConnector()

    assert connector._subdomain == "acme"
    assert connector._user == "agent@example.com"
    assert connector._token == "secret"
    connector.close()


def test_constructor_requires_user(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.delenv("ZENDESKTICKET_USER", raising=False)
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    with pytest.raises(ValueError, match="Zendesk tickets credentials required"):
        ZendeskTicketsConnector()


def test_build_manifest_is_empty_when_api_returns_no_tickets(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")
    state_dir = _make_state_dir("empty-manifest")

    connector = ZendeskTicketsConnector(state_dir=str(state_dir))
    connector._http = FakeHTTPClient(pages=[{"tickets": [], "next_page": None}], comments={})

    assert connector.build_manifest() == []
    connector.close()


def test_build_manifest_uses_min_datetime_when_checkpoint_missing(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")
    monkeypatch.delenv("ZENDESKTICKET_PAGE_SIZE", raising=False)
    state_dir = _make_state_dir("missing-checkpoint")
    checkpoint_file = state_dir / "resume_checkpoint.txt"

    connector = ZendeskTicketsConnector(state_dir=str(state_dir))
    connector._http = FakeHTTPClient(
        pages=[
            {
                "tickets": [
                    {
                        "id": 1001,
                        "subject": "Printer down",
                        "description": "Cannot print boarding passes",
                        "status": "open",
                        "priority": "normal",
                        "updated_at": "2024-01-02T03:04:05Z",
                        "tags": ["ops"],
                    }
                ],
                "next_page": None,
            }
        ],
        comments={1001: []},
    )

    entries = connector.build_manifest()

    assert [entry.filename for entry in entries] == ["1001.md"]
    assert connector._http.calls[0] == {
        "path": "/tickets.json",
        "params": {
            "page": 1,
            "per_page": 10,
            "sort_by": "updated_at",
            "sort_order": "asc",
            "start_time": 0,
        },
    }
    assert checkpoint_file.read_text().strip() == "2024-01-02T03:04:05Z"
    connector.close()


def test_build_manifest_filters_by_checkpoint_and_renders_comments(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")
    monkeypatch.setenv("ZENDESKTICKET_PAGE_SIZE", "1")
    state_dir = _make_state_dir("filtered-checkpoint")
    checkpoint_file = state_dir / "resume_checkpoint.txt"
    checkpoint_file.write_text("2024-01-01T00:00:00Z")

    connector = ZendeskTicketsConnector(state_dir=str(state_dir))
    connector._http = FakeHTTPClient(
        pages=[
            {
                "tickets": [
                    {
                        "id": 1001,
                        "subject": "Printer down",
                        "description": "Cannot print boarding passes",
                        "status": "open",
                        "priority": "normal",
                        "updated_at": "2024-01-02T03:04:05Z",
                        "tags": ["ops"],
                    }
                ],
                "next_page": "page-2",
            },
            {
                "tickets": [
                    {
                        "id": 1002,
                        "subject": "Badge reader offline",
                        "description": "Front desk badge reader stopped responding",
                        "status": "pending",
                        "priority": "high",
                        "updated_at": "2024-01-03T04:05:06Z",
                        "tags": ["facilities"],
                    }
                ],
                "next_page": None,
            },
        ],
        comments={
            1001: [
                {
                    "id": 501,
                    "author_id": 2001,
                    "created_at": "2024-01-02T03:10:00Z",
                    "body": "Investigating the printer queue.",
                }
            ],
            1002: [],
        },
    )

    entries = connector.build_manifest()
    text = connector.read_file("", "1001.md").decode()

    assert [entry.filename for entry in entries] == ["1001.md", "1002.md"]
    assert connector._http.calls[0] == {
        "path": "/tickets.json",
        "params": {
            "page": 1,
            "per_page": 1,
            "sort_by": "updated_at",
            "sort_order": "asc",
            "start_time": 1704067200,
        },
    }
    assert text.startswith("# Ticket 1001: Printer down")
    assert "Updated at: 2024-01-02T03:04:05Z" in text
    assert "## Comments" in text
    assert "Investigating the printer queue." in text
    assert checkpoint_file.read_text().strip() == "2024-01-03T04:05:06Z"
    connector.close()


def test_build_manifest_advances_checkpoint_for_filtered_ticket(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")
    monkeypatch.setenv("ZENDESKTICKET_INCLUDETAGS", "ops")
    state_dir = _make_state_dir("filtered-later-ticket")
    checkpoint_file = state_dir / "resume_checkpoint.txt"

    connector = ZendeskTicketsConnector(state_dir=str(state_dir))
    connector._http = FakeHTTPClient(
        pages=[
            {
                "tickets": [
                    {
                        "id": 1001,
                        "subject": "Printer down",
                        "description": "Cannot print boarding passes",
                        "status": "open",
                        "priority": "normal",
                        "updated_at": "2024-01-02T03:04:05Z",
                        "tags": ["ops"],
                    },
                    {
                        "id": 1002,
                        "subject": "Badge reader offline",
                        "description": "Front desk badge reader stopped responding",
                        "status": "pending",
                        "priority": "high",
                        "updated_at": "2024-01-03T04:05:06Z",
                        "tags": ["facilities"],
                    },
                ],
                "next_page": None,
            }
        ],
        comments={1001: []},
    )

    entries = connector.build_manifest()

    assert [entry.filename for entry in entries] == ["1001.md"]
    assert checkpoint_file.read_text().strip() == "2024-01-03T04:05:06Z"
    connector.close()


def test_read_file_raises_file_not_found(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    connector = ZendeskTicketsConnector()

    with pytest.raises(FileNotFoundError):
        connector.read_file("", "missing.txt")
    connector.close()


def test_close_closes_http_client(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    connector = ZendeskTicketsConnector()
    assert connector._http.is_closed is False

    connector.close()

    assert connector._http.is_closed is True
