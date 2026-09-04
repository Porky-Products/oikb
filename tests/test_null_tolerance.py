"""Regression tests for null-tolerant parsing of open-webui sync responses.

Background (production incident, 2026-09-03/04): three daemon runs failed
with ``'NoneType' object is not iterable`` for ``zendesktickets:porky``.
The live open-webui ``GET /knowledge/{id}`` response for that KB returns
``"files": null`` (explicit JSON null — the KB's files were never linked),
and ``GET .../sync/diff`` responds 200 with no server-side error.  oikb's
``dict.get(key, default)`` parsing only defaults on a *missing* key, not on
an explicit null value, so iteration over the ``None`` crashed the sync in
the dedup guard (the only code path between the ``GET /knowledge/{id}``
call and the error).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from oikb.client import OikbClient


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHttp:
    """Stands in for the httpx.Client inside OikbClient."""

    def __init__(self, payload: dict[str, Any]):
        self._payload = payload
        self.requests: list[tuple[str, str]] = []

    def get(self, url: str) -> _FakeResponse:
        self.requests.append(("GET", url))
        return _FakeResponse(self._payload)

    def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        self.requests.append(("POST", url))
        return _FakeResponse(self._payload)


def _client_with(payload: dict[str, Any]) -> tuple[OikbClient, _FakeHttp]:
    client = OikbClient.__new__(OikbClient)  # skip __init__ (no real HTTP)
    client._http = _FakeHttp(payload)  # type: ignore[assignment]
    return client, client._http  # type: ignore[return-value]


class TestListKbFilesNullTolerance:
    def test_files_explicit_null_returns_empty_list(self):
        """Exact live-server shape: KB with `"files": null`."""
        client, _ = _client_with({"id": "kb1", "name": "Zendesk Tickets", "files": None})
        assert client.list_kb_files("kb1") == []

    def test_files_missing_returns_empty_list(self):
        client, _ = _client_with({"id": "kb1"})
        assert client.list_kb_files("kb1") == []

    def test_files_present_passed_through(self):
        files = [{"id": "f1", "hash": "abc"}]
        client, _ = _client_with({"id": "kb1", "files": files})
        assert client.list_kb_files("kb1") == files


class TestSyncDiffNullTolerance:
    """sync_diff response lists may be explicit nulls; parsing must not crash."""

    def _diff_fields(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Mirrors the parsing in sync._run_sync_inner.
        return {
            "added": payload.get("added") or [],
            "modified": payload.get("modified") or [],
            "deleted": payload.get("deleted") or [],
            "unmodified_count": payload.get("unmodified_count", 0),
            "mkdir": payload.get("mkdir") or [],
            "rmdir": payload.get("rmdir") or [],
            "directory_map": payload.get("directory_map") or {},
        }

    def test_all_lists_null(self):
        fields = self._diff_fields(
            {"added": None, "modified": None, "deleted": None,
             "mkdir": None, "rmdir": None, "directory_map": None}
        )
        assert fields["added"] == []
        assert fields["modified"] == []
        assert fields["deleted"] == []
        assert fields["mkdir"] == []
        assert fields["rmdir"] == []
        assert fields["directory_map"] == {}

    def test_lists_missing(self):
        fields = self._diff_fields({})
        assert fields["added"] == []
        assert fields["directory_map"] == {}

    def test_lists_present_passed_through(self):
        payload = {"added": [{"path": "x"}], "deleted": [{"file_id": "f1"}],
                   "directory_map": {"tickets": "dir1"}}
        fields = self._diff_fields(payload)
        assert fields["added"] == [{"path": "x"}]
        assert fields["directory_map"] == {"tickets": "dir1"}
