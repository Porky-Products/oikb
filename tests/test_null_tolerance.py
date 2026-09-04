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
from oikb.connectors import BaseConnector, ManifestEntry
from oikb.sync import SyncResult, _run_sync_inner


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


class _NullDiffClient(OikbClient):
    """Fake client whose sync_diff returns explicit-null diff lists."""

    def __init__(self, diff: dict[str, Any] | None):
        self._diff = diff if diff is not None else {}

    def sync_diff(self, kb_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        return self._diff

    def sync_cleanup(self, kb_id: str, file_ids: list[str], rmdir: list[str] | None) -> Any:
        return {}

    def create_directory(self, kb_id: str, name: str, parent_id: str | None) -> dict[str, Any]:
        return {"id": "new-dir"}

    def list_kb_files(self, kb_id: str) -> list[dict[str, Any]]:
        return []


class _StubConnector(BaseConnector):
    """Minimal content-addressed connector with a one-file manifest."""

    content_addressed_checksums = True

    def __init__(self, manifest: list[ManifestEntry] | None = None):
        self._manifest = manifest if manifest is not None else [
            ManifestEntry(filename="t.md", path="", checksum="c1", size=10)
        ]

    def build_manifest(self) -> list[ManifestEntry]:
        return list(self._manifest)

    def read_file(self, path: str, filename: str) -> bytes:
        return b"content"

    def upload_file(self, kb_id: str, filename: str, content: bytes,
                     path: str = "", parent_id: str | None = None) -> dict[str, Any]:
        return {"id": "new-file"}


def _run_null_sync(diff: dict[str, Any] | None) -> SyncResult:
    """Drive the real _run_sync_inner with a stub connector and null diff."""
    return _run_sync_inner(
        client=_NullDiffClient(diff),
        connector=_StubConnector(),
        kb_id="kb1",
        dry_run=True,
        verbose=False,
        quiet=True,
        manifest_filter=None,
        concurrency=1,
        result=SyncResult(),
        cancel_requested=None,
    )


class TestSyncDiffNullTolerance:
    """sync_diff response lists may be explicit nulls; parsing must not crash."""

    def test_all_lists_null(self):
        # Exact failure shape from the production incident: sync_diff
        # responds 200 with all list fields as explicit JSON nulls.
        # Drives the real _run_sync_inner; reverting sync.py's parsing
        # to dict.get(key, default) must fail this test.
        result = _run_null_sync(
            {"added": None, "modified": None, "deleted": None,
             "mkdir": None, "rmdir": None, "directory_map": None}
        )
        assert result.added == 0
        assert result.modified == 0
        assert result.deleted == 0
        assert result.dirs_created == 0
        assert result.dirs_removed == 0

    def test_lists_missing(self):
        result = _run_null_sync({})
        assert result.added == 0
        assert result.dirs_created == 0

    def test_lists_present_passed_through(self):
        payload = {"added": [{"path": "", "filename": "t.md", "checksum": "c1", "size": 10}],
                   "deleted": [{"file_id": "f1", "filename": "old.md", "path": ""}],
                   "directory_map": {"tickets": "dir1"}}
        result = _run_null_sync(payload)
        assert result.added == 1
        assert result.deleted == 1
