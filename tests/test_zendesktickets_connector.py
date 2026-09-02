from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import httpx


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from oikb.connectors import ManifestEntry
from oikb.connectors.zendesktickets import ZendeskTicketsConnector, parse_zendesktickets_source
from oikb.sync import run_sync


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, headers: dict[str, str] | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://acme.zendesk.com/api/v2/mock")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=request, response=response)
        return None

    def json(self) -> dict:
        return self._payload


class FakeHTTPClient:
    def __init__(self, ticket_pages: list[dict], comments: dict[int, list[dict]] | None = None, attachments: dict[str, bytes] | None = None, comment_status_codes: dict[int, list[int]] | None = None):
        self._ticket_pages = list(ticket_pages)
        self._comments = comments or {}
        self._attachments = attachments or {}
        self._comment_status_codes: dict[int, list[int]] = comment_status_codes or {}
        self.calls: list[dict] = []
        self.is_closed = False

    def get(self, path: str, params: dict | None = None) -> FakeResponse:
        self.calls.append({"path": path, "params": params})
        if path == "/incremental/tickets/cursor.json":
            if not self._ticket_pages:
                raise AssertionError("No more ticket pages configured")
            return FakeResponse(self._ticket_pages.pop(0))
        if path.endswith("/comments.json"):
            ticket_id = int(path.split("/")[2])
            if ticket_id in self._comment_status_codes and self._comment_status_codes[ticket_id]:
                status = self._comment_status_codes[ticket_id].pop(0)
                return FakeResponse({}, status_code=status)
            return FakeResponse({"comments": self._comments.get(ticket_id, [])})
        if path.startswith("https://attachments.example/") or path.startswith("https://acme.zendesk.com/attachments/"):
            name = path.rsplit("/", 1)[-1]
            return FakeBinaryResponse(self._attachments[name])
        raise AssertionError(f"Unexpected request: {path}")

    def close(self) -> None:
        self.is_closed = True


class FakeBinaryResponse:
    def __init__(self, content: bytes, status_code: int = 200, headers: dict[str, str] | None = None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://acme.zendesk.com/attachments/mock")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=request, response=response)
        return None


class FakeClient:
    def __init__(self, existing_files: list[dict] | None = None):
        self.existing_files = existing_files or []
        self.diff_calls: list[list[dict]] = []
        self.cleanup_calls: list[dict] = []
        self.upload_calls: list[dict] = []
        self.directory_calls: list[dict] = []

    def sync_diff(self, kb_id: str, manifest: list[dict]) -> dict:
        self.diff_calls.append(manifest)
        existing_map = {(f["path"], f["filename"]): f for f in self.existing_files}
        manifest_map = {(f["path"], f["filename"]): f for f in manifest}

        added = []
        modified = []
        deleted = []
        mkdir = sorted({item["path"] for item in manifest if item["path"] and item["path"] not in {f["path"] for f in self.existing_files}})

        for key, entry in manifest_map.items():
            if key not in existing_map:
                added.append({"path": entry["path"], "filename": entry["filename"]})
            elif existing_map[key]["checksum"] != entry["checksum"]:
                modified.append(
                    {
                        "path": entry["path"],
                        "filename": entry["filename"],
                        "stale_file_id": existing_map[key]["file_id"],
                    }
                )

        for key, entry in existing_map.items():
            if key not in manifest_map:
                deleted.append(
                    {
                        "path": entry["path"],
                        "filename": entry["filename"],
                        "file_id": entry["file_id"],
                    }
                )

        return {
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "unmodified_count": len(existing_map) - len(modified),
            "mkdir": mkdir,
            "rmdir": [],
            "directory_map": {},
        }

    def sync_cleanup(self, kb_id: str, file_ids: list[str], dir_ids: list[str] | None = None) -> dict:
        self.cleanup_calls.append({"kb_id": kb_id, "file_ids": file_ids, "dir_ids": dir_ids})
        return {}

    def create_directory(self, kb_id: str, name: str, parent_id: str | None = None) -> dict:
        dir_id = f"dir-{len(self.directory_calls) + 1}"
        self.directory_calls.append({"kb_id": kb_id, "name": name, "parent_id": parent_id, "id": dir_id})
        return {"id": dir_id}

    def upload_file(self, file_content: bytes, filename: str, kb_id: str, file_hash: str, directory_id: str | None = None) -> dict:
        self.upload_calls.append(
            {
                "filename": filename,
                "kb_id": kb_id,
                "file_hash": file_hash,
                "directory_id": directory_id,
                "content": file_content,
            }
        )
        return {}


def _make_state_dir(tmp_path: Path, name: str) -> Path:
    state_dir = tmp_path / name
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _ticket(ticket_id: int, updated_at: str, *, subject: str = "Printer down", tags: list[str] | None = None, description: str = "Cannot print boarding passes", status: str = "open", priority: str = "normal", attachments: list[dict] | None = None) -> dict:
    return {
        "id": ticket_id,
        "subject": subject,
        "description": description,
        "status": status,
        "priority": priority,
        "updated_at": updated_at,
        "tags": tags or [],
        "attachments": attachments or [],
    }


def _comment(comment_id: int, body: str, *, created_at: str = "2024-01-02T03:10:00Z", author_id: int = 2001, attachments: list[dict] | None = None) -> dict:
    return {
        "id": comment_id,
        "author_id": author_id,
        "created_at": created_at,
        "body": body,
        "attachments": attachments or [],
    }


def _attachment(name: str, *, url: str | None = None) -> dict:
    return {
        "file_name": name,
        "content_url": url or f"https://attachments.example/{name}",
    }


def _build_connector(monkeypatch: pytest.MonkeyPatch, state_dir: Path, *, pages: list[dict], comments: dict[int, list[dict]] | None = None, attachments: dict[str, bytes] | None = None) -> ZendeskTicketsConnector:
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")
    connector = ZendeskTicketsConnector(state_dir=str(state_dir))
    connector._http = FakeHTTPClient(ticket_pages=pages, comments=comments, attachments=attachments)
    return connector


def test_parse_zendesktickets_source_requires_subdomain():
    assert parse_zendesktickets_source("zendesktickets:acme") == {"subdomain": "acme"}


def test_constructor_requires_all_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.delenv("ZENDESKTICKET_USER", raising=False)
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    with pytest.raises(ValueError, match="Zendesk tickets credentials required"):
        ZendeskTicketsConnector()


def test_build_manifest_uses_min_datetime_when_checkpoint_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "missing-checkpoint")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z", tags=["ops"])], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: []},
    )

    entries = connector.build_manifest()

    assert [entry.filename for entry in entries] == ["1001.md"]
    assert connector._http.calls[0]["params"]["start_time"] == 0
    connector.close()


def test_build_manifest_renders_comments_and_persists_state_after_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "render-comments")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z", tags=["ops"])], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: [_comment(501, "Investigating the printer queue.")]},
    )

    manifest = connector.build_manifest()
    text = connector.read_file("tickets", "1001.md").decode()

    assert manifest == [ManifestEntry(filename="1001.md", path="tickets", checksum=manifest[0].checksum, size=len(text.encode("utf-8")))]
    assert "## Comments" in text
    assert "Investigating the printer queue." in text
    assert "Updated at: 2024-01-02T03:04:05Z" in text
    connector.mark_sync_complete()
    assert (state_dir / "resume_cursor.txt").read_text().strip() == "cursor-1"
    connector.close()


def test_aggressive_checkpoint_saves_state_before_cursor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Aggressive in-run saves must persist manifest state, not just the cursor.

    Persisting the cursor alone means a failed sync afterward resumes PAST the
    newly built tickets whose entries were never in manifest_state.json —
    permanent omission, since run-cache restoration needs an existing manifest
    entry. State and cursor are now written together at the page boundary.
    """
    state_dir = _make_state_dir(tmp_path, "aggressive-state-before-cursor")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T02:00:00Z")
    cursor_path = state_dir / "resume_cursor.txt"
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_of_stream": False,
                "after_cursor": "cursor-A",
            },
            {
                "tickets": [_ticket(1002, "2024-01-02T04:04:05Z")],
                "end_of_stream": True,
                "after_cursor": "cursor-B",
            },
        ],
        comments={1001: [], 1002: []},
    )

    connector.build_manifest()

    # Simulate sync failure: entries for 1001 (page-1 boundary) must already be
    # in manifest_state.json alongside the page-1 cursor.
    saved_state = json.loads((state_dir / "manifest_state.json").read_text())
    assert "1001" in saved_state["ticket_files"]
    assert saved_state["cursor"] == "cursor-B"
    assert cursor_path.read_text().strip() == "cursor-B"
    # The legacy datetime checkpoint is a bootstrap input only: never rewritten.
    assert checkpoint_path.read_text().strip() == "2024-01-02T02:00:00Z"
    connector.close()


def test_aggressive_state_saved_on_non_advancing_cursor_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A page whose after_cursor matches the previous page still saves state.

    Zendesk can re-serve the same opaque cursor on consecutive pages (e.g.
    heavy update bursts around a boundary). The duplicate-cursor guard stops
    pagination, but that page's manifest entries must still reach
    manifest_state.json — otherwise a sync failure resumes past records that
    exist only in memory.
    """
    state_dir = _make_state_dir(tmp_path, "aggressive-non-advancing")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T02:00:00Z")
    cursor_path = state_dir / "resume_cursor.txt"
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_of_stream": False,
                "after_cursor": "cursor-A",
            },
            {
                "tickets": [_ticket(1002, "2024-01-02T04:04:05Z")],
                "end_of_stream": False,
                "after_cursor": "cursor-A",
            },
        ],
        comments={1001: [], 1002: []},
    )

    connector.build_manifest()

    # Duplicate-cursor guard stopped pagination after page 2, but page 2's
    # ticket 1002 must still be persisted, and the persisted cursor matched.
    saved_state = json.loads((state_dir / "manifest_state.json").read_text())
    assert "1002" in saved_state["ticket_files"]
    assert saved_state["cursor"] == "cursor-A"
    assert cursor_path.read_text().strip() == "cursor-A"
    # At-least-once semantic: the duplicate page is re-served next run and
    # deduplicated by checksum — the guard only prevents an infinite loop.
    assert checkpoint_path.read_text().strip() == "2024-01-02T02:00:00Z"
    connector.close()


def test_missing_after_cursor_on_end_of_stream_page_tolerated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """An absent after_cursor on an end_of_stream page must not fail the run.

    Zendesk marks the final page with end_of_stream=true; its after_cursor has
    no resume role. Some payloads omit it on the last page. Absence combined
    with end_of_stream=true is therefore tolerated: the run completes, but no
    cursor file is written (there is nothing to resume from). The legacy
    datetime checkpoint file is never rewritten either — it is a bootstrap
    input only.
    """
    state_dir = _make_state_dir(tmp_path, "end-of-stream-no-cursor")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    cursor_path = state_dir / "resume_cursor.txt"
    checkpoint_path.write_text("2024-01-02T03:00:00Z")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T02:30:00Z")],
                "end_of_stream": True,
            },
        ],
        comments={1001: []},
    )

    manifest = [entry.display_path for entry in connector.build_manifest()]
    assert manifest == ["tickets/1001.md"]
    connector.mark_sync_complete()

    assert not cursor_path.exists()
    assert checkpoint_path.read_text().strip() == "2024-01-02T03:00:00Z"
    connector.close()


def test_state_write_is_atomic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Interrupted state writes must never corrupt on-disk JSON.

    manifest_state.json is a crash-recovery input. A truncated write (process
    death mid-write_text) would make the next run fail in _load_state before
    it can use the preserved checkpoint and run cache. The write goes through
    temp-file + os.replace; killing write_text after the first partial byte
    leaves the original state intact.
    """
    state_dir = _make_state_dir(tmp_path, "atomic-write")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    good_page = {
        "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
        "end_of_stream": True, "after_cursor": "cursor-1",
    }
    connector = _build_connector(monkeypatch, state_dir, pages=[good_page], comments={1001: []})
    connector.build_manifest()
    connector.mark_sync_complete()
    connector.close()

    state_path = state_dir / "manifest_state.json"
    tmp_path = state_dir / "manifest_state.json.tmp"
    good = state_path.read_text()

    class Boom(RuntimeError):
        pass

    original = Path.write_text

    def partial_write(self, data, *args, **kwargs):
        if self == tmp_path:
            # Simulate process death mid-write: partial bytes, no replace.
            raise Boom
        return original(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", partial_write)

    connector2 = _build_connector(monkeypatch, state_dir, pages=[good_page], comments={1001: []})
    with pytest.raises(Boom):
        connector2.build_manifest()
    connector2.close()

    # The original good state survived the interrupted write.
    assert json.loads(state_path.read_text()) == json.loads(good)
    assert not (state_dir / "manifest_state.json.tmp").exists()


def test_aggressive_state_save_stamps_run_start_and_carries_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Mid-run state saves stamp the run-start checkpoint and carry the cursor.

    With no cursor file yet, a mid-run failure resumes via the state file's
    cursor. The state's checkpoint stamp must remain the run-start value (a
    later datetime could skip unserved records once the cursor file exists),
    while the boundary cursor is written into state["cursor"] so the next
    run resumes exactly at the page boundary.
    """
    state_dir = _make_state_dir(tmp_path, "first-cursor-run-start")
    cursor_path = state_dir / "resume_cursor.txt"
    state = {
        "checkpoint": "2024-01-02T02:00:00Z",
        "ticket_files": {},
        "seen_ticket_ids": [],
        "excluded_ticket_ids": [],
        "attachments_enabled": False,
    }
    (state_dir / "manifest_state.json").write_text(json.dumps(state))
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T05:05:05Z")],
                "end_of_stream": False,
                "after_cursor": "cursor-A",
            },
            {
                "tickets": [_ticket(1002, "2024-01-02T03:04:05Z")],
                "end_of_stream": True,
                "after_cursor": "cursor-B",
            },
        ],
        comments={1001: [], 1002: []},
    )

    connector.build_manifest()

    # Mid-run state (as of page 2's boundary) must carry the run-start
    # checkpoint and the boundary cursor.
    saved_state = json.loads((state_dir / "manifest_state.json").read_text())
    assert saved_state["checkpoint"] == "2024-01-02T02:00:00Z"
    assert saved_state["cursor"] == "cursor-B"
    assert "1002" in saved_state["ticket_files"]
    assert cursor_path.read_text().strip() == "cursor-B"
    connector.close()


def test_sync_failure_does_not_advance_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "checkpoint-on-success")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: []},
    )

    connector._http._ticket_pages.append({"tickets": [], "end_of_stream": True, "after_cursor": "cursor-1"})
    connector.build_manifest()

    assert not (state_dir / "resume_cursor.txt").exists()
    connector.close()


def test_aggressive_checkpoint_keeps_run_cache_after_failure_for_resume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "aggressive-cache-preserve")
    cursor_path = state_dir / "resume_cursor.txt"
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1001: []},
    )

    connector.build_manifest()

    # Simulate failed sync: close() is always called (even on failure) by run_sync().
    # With aggressive checkpointing and a cursor file present, close() should NOT
    # delete .run-cache so the next run can resume.
    assert cursor_path.exists()
    run_cache = state_dir / ".run-cache"
    assert run_cache.exists()
    sentinel = run_cache / "preserve.me"
    sentinel.write_text("keep")

    connector.close()

    # .run-cache must survive close() when aggressive checkpoint is active.
    assert sentinel.exists()

    # A new connector instance picks up the preserved cache and resumes via cursor.
    connector2 = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: []},
    )
    connector2.build_manifest()
    assert cursor_path.exists()
    connector2.close()


def test_sync_run_advances_cursor_after_completed_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "run-sync-checkpoint")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: []},
    )
    client = FakeClient()

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert result.added == 1
    assert (state_dir / "resume_cursor.txt").read_text().strip() == "cursor-1"


def test_sync_dry_run_does_not_advance_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "dry-run-no-checkpoint")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "false")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: []},
    )
    client = FakeClient()

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True, dry_run=True)

    assert result.added == 1
    assert not (state_dir / "resume_cursor.txt").exists()


def test_sync_exception_does_not_advance_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "exception-no-checkpoint")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "false")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: []},
    )

    class FailingClient(FakeClient):
        def sync_diff(self, kb_id: str, manifest: list[dict]) -> dict:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_sync(client=FailingClient(), connector=connector, kb_id="kb-1", quiet=True)

    assert not (state_dir / "resume_cursor.txt").exists()


def test_sync_advances_checkpoint_even_when_some_uploads_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Checkpoint must advance after a completed run even if individual uploads errored.

    Upload errors are already retried 3× by the upload loop.  Blocking the
    checkpoint on remaining errors would cause every subsequent run to
    re-process the full ticket set from the same start_time, compounding the
    problem instead of making forward progress.
    """
    state_dir = _make_state_dir(tmp_path, "checkpoint-despite-upload-error")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: []},
    )

    class UploadFailingClient(FakeClient):
        def upload_file(self, file_content: bytes, filename: str, kb_id: str, file_hash: str, directory_id: str | None = None) -> dict:
            raise httpx.HTTPStatusError("500", request=httpx.Request("POST", "https://example.com"), response=httpx.Response(500))

    result = run_sync(client=UploadFailingClient(), connector=connector, kb_id="kb-1", quiet=True)

    assert result.errors  # upload failed
    # Cursor must still have advanced.
    assert (state_dir / "resume_cursor.txt").read_text().strip() == "cursor-1"


def test_build_manifest_includes_previously_synced_unchanged_ticket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "carry-forward")
    (state_dir / "manifest_state.json").write_text(
        json.dumps(
            {
                "checkpoint": "2024-01-01T00:00:00Z",
                "attachments_enabled": False,
                "ticket_files": {
                    "1001": {
                        "updated_at": "2024-01-01T00:00:00Z",
                        "entries": [
                            {
                                "filename": "1001.md",
                                "path": "tickets",
                                "checksum": "abc123",
                                "size": 12,
                            }
                        ],
                    }
                },
            }
        )
    )
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1002: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md", "tickets/1002.md"]
    connector.close()


def _write_prior_state(state_dir: Path, ticket_id: int, checksum: str, size: int, updated_at: str = "2024-01-01T00:00:00Z") -> None:
    state_dir.joinpath("manifest_state.json").write_text(
        json.dumps(
            {
                "checkpoint": "2024-01-01T00:00:00Z",
                "attachments_enabled": False,
                "ticket_files": {
                    str(ticket_id): {
                        "updated_at": updated_at,
                        "entries": [
                            {
                                "filename": f"{ticket_id}.md",
                                "path": "tickets",
                                "checksum": checksum,
                                "size": size,
                            }
                        ],
                    }
                },
            }
        )
    )
    # A prior completed run always leaves a checkpoint behind; the cache is
    # only preserved when this file exists.
    state_dir.joinpath("resume_checkpoint.txt").write_text("2024-01-01T00:00:00Z")


def test_read_file_restores_carried_forward_entry_from_run_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "lazy-restore")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    content = b"# Ticket 1001\n\nPrinter down\n"

    checksum = hashlib.sha256(content).hexdigest()[:16]
    _write_prior_state(state_dir, 1001, checksum=checksum, size=len(content))

    # Prior run wrote the ticket markdown into .run-cache; aggressive
    # checkpointing preserved it after close().
    run_cache = state_dir / ".run-cache"
    run_cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(b"tickets/1001.md").hexdigest()
    (run_cache / key).write_bytes(content)

    # Current run sees only a newer ticket (1002); 1001 is carried forward
    # from prior state without being re-downloaded.
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1002: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md", "tickets/1002.md"]
    assert connector.read_file("tickets", "1001.md") == content
    # Second read (e.g. upload retry) must hit the in-memory cache, not disk:
    # delete the backing cache file and read again.
    connector._run_cache_dir.joinpath(hashlib.sha256(b"tickets/1001.md").hexdigest()).unlink()
    assert connector.read_file("tickets", "1001.md") == content
    connector.close()


def test_read_file_raises_when_run_cache_content_mismatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path, "lazy-restore-mismatch")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    stale_content = b"# Ticket 1001\n\nPrinter jam \n"  # same length as real_content, different bytes
    real_content = b"# Ticket 1001\n\nPrinter down\n"

    checksum = hashlib.sha256(real_content).hexdigest()[:16]
    _write_prior_state(state_dir, 1001, checksum=checksum, size=len(real_content))

    run_cache = state_dir / ".run-cache"
    run_cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(b"tickets/1001.md").hexdigest()
    (run_cache / key).write_bytes(stale_content)  # corrupted / divergent leftover

    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1002: []},
    )

    connector.build_manifest()

    with pytest.raises(FileNotFoundError):
        connector.read_file("tickets", "1001.md")
    connector.close()


def test_read_file_rejects_wrong_size_cache_file_without_reading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path, "lazy-restore-oversized")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    real_content = b"# Ticket 1001\n\nPrinter down\n"
    checksum = hashlib.sha256(real_content).hexdigest()[:16]
    _write_prior_state(state_dir, 1001, checksum=checksum, size=len(real_content))

    run_cache = state_dir / ".run-cache"
    run_cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(b"tickets/1001.md").hexdigest()
    # Oversized leftover (e.g. truncated write or a different entry's bytes).
    oversized = real_content + b"x" * 1024
    (run_cache / key).write_bytes(oversized)

    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1002: []},
    )

    connector.build_manifest()

    with pytest.raises(FileNotFoundError):
        connector.read_file("tickets", "1001.md")
    connector.close()


def test_read_file_raises_when_run_cache_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "lazy-restore-missing-cache")
    _write_prior_state(state_dir, 1001, checksum="deadbeefdeadbeef", size=10)

    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1002: []},
    )

    connector.build_manifest()

    with pytest.raises(FileNotFoundError):
        connector.read_file("tickets", "1001.md")
    connector.close()



def test_include_and_exclude_tags_filter_ticket_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "tag-filter")
    monkeypatch.setenv("ZENDESKTICKET_INCLUDETAGS", "ops,urgent")
    monkeypatch.setenv("ZENDESKTICKET_EXCLUDETAGS", "ignore-me")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T03:04:05Z", tags=["ops"]),
                    _ticket(1002, "2024-01-02T03:05:05Z", tags=["facilities"]),
                    _ticket(1003, "2024-01-02T03:06:05Z", tags=["urgent", "ignore-me"]),
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1001: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    connector.close()


def test_singular_tag_env_vars_are_supported_for_compatibility(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "singular-tag-filter")
    monkeypatch.delenv("ZENDESKTICKET_INCLUDETAGS", raising=False)
    monkeypatch.delenv("ZENDESKTICKET_EXCLUDETAGS", raising=False)
    monkeypatch.setenv("ZENDESKTICKET_INCLUDETAG", "ops,urgent")
    monkeypatch.setenv("ZENDESKTICKET_EXCLUDETAG", "ignore-me")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T03:04:05Z", tags=["ops"]),
                    _ticket(1002, "2024-01-02T03:05:05Z", tags=["facilities"]),
                    _ticket(1003, "2024-01-02T03:06:05Z", tags=["urgent", "ignore-me"]),
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1001: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    connector.close()


def test_status_filter_includes_only_configured_statuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "status-filter")
    monkeypatch.setenv("ZENDESKTICKET_STATUS", "solved, closed")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T03:04:05Z", status="open"),
                    _ticket(1002, "2024-01-02T03:05:05Z", status="solved"),
                    _ticket(1003, "2024-01-02T03:06:05Z", status="closed"),
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1002: [], 1003: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1002.md", "tickets/1003.md"]
    connector.close()


def test_filtered_ticket_is_removed_from_kb_on_next_sync(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "filtered-removal")
    existing_files = [
        {"path": "tickets", "filename": "1001.md", "checksum": "old", "file_id": "file-1"},
        {"path": "attachments/1001", "filename": "1001-screenshot.png", "checksum": "old2", "file_id": "file-2"},
    ]
    (state_dir / "manifest_state.json").write_text(
        json.dumps(
            {
                "checkpoint": "2024-01-01T00:00:00Z",
                "attachments_enabled": True,
                "ticket_files": {
                    "1001": {
                        "updated_at": "2024-01-01T00:00:00Z",
                        "entries": [
                            {"path": "tickets", "filename": "1001.md", "checksum": "old", "size": 10},
                            {"path": "attachments/1001", "filename": "1001-screenshot.png", "checksum": "old2", "size": 4},
                        ],
                    }
                },
            }
        )
    )
    monkeypatch.setenv("ZENDESKTICKET_INCLUDETAGS", "ops")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z", tags=["facilities"])], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={},
    )
    client = FakeClient(existing_files=existing_files)

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert client.cleanup_calls == [{"kb_id": "kb-1", "file_ids": ["file-1", "file-2"], "dir_ids": None}]
    assert result.deleted == 2


def test_attachments_are_added_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "attachments-enabled")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(
                        1001,
                        "2024-01-02T03:04:05Z",
                        attachments=[_attachment("error-log.txt", url="https://acme.zendesk.com/attachments/error-log.txt")],
                    )
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={
            1001: [
                _comment(
                    501,
                    "See screenshot",
                    attachments=[_attachment("screenshot.png", url="https://acme.zendesk.com/attachments/screenshot.png")],
                )
            ]
        },
        attachments={"error-log.txt": b"log-bytes", "screenshot.png": b"image-bytes"},
    )

    manifest = connector.build_manifest()

    assert sorted(entry.display_path for entry in manifest) == [
        "attachments/1001/1001-91561f-error-log.txt",
        "attachments/1001/1001-e39f8d-screenshot.png",
        "tickets/1001.md",
    ]
    assert connector.read_file("attachments/1001", "1001-e39f8d-screenshot.png") == b"image-bytes"
    connector.close()


def test_disabling_attachments_removes_prior_attachment_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "attachments-disabled")
    (state_dir / "manifest_state.json").write_text(
        json.dumps(
            {
                "checkpoint": "2024-01-01T00:00:00Z",
                "attachments_enabled": True,
                "ticket_files": {
                    "1001": {
                        "updated_at": "2024-01-01T00:00:00Z",
                        "entries": [
                            {"path": "tickets", "filename": "1001.md", "checksum": "md", "size": 10},
                            {"path": "attachments/1001", "filename": "1001-screenshot.png", "checksum": "img", "size": 5},
                        ],
                    }
                },
            }
        )
    )
    existing_files = [
        {"path": "tickets", "filename": "1001.md", "checksum": "md", "file_id": "file-md"},
        {"path": "attachments/1001", "filename": "1001-screenshot.png", "checksum": "img", "file_id": "file-img"},
    ]
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "false")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: []},
    )
    client = FakeClient(existing_files=existing_files)

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert result.deleted == 1
    assert [upload["filename"] for upload in client.upload_calls] == ["1001.md"]
    assert client.cleanup_calls == [{"kb_id": "kb-1", "file_ids": ["file-img", "file-md"], "dir_ids": None}]


def test_attachments_with_same_name_use_content_hashes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "attachments-same-name")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(
                        1001,
                        "2024-01-02T03:04:05Z",
                        attachments=[
                            _attachment("screenshot.png", url="https://acme.zendesk.com/attachments/screenshot-1.png"),
                            _attachment("screenshot.png", url="https://acme.zendesk.com/attachments/screenshot-2.png"),
                        ],
                    )
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1001: []},
        attachments={"screenshot-1.png": b"one-bytes", "screenshot-2.png": b"two-bytes"},
    )

    manifest = connector.build_manifest()

    assert sorted(entry.display_path for entry in manifest) == [
        "attachments/1001/1001-1fa9b9-screenshot.png",
        "attachments/1001/1001-4311eb-screenshot.png",
        "tickets/1001.md",
    ]
    connector.close()


def test_no_attachment_directory_created_when_no_attachments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "attachments-empty")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: []},
    )
    client = FakeClient(existing_files=[])

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert result.dirs_created == 1
    assert client.directory_calls == [{"kb_id": "kb-1", "name": "tickets", "parent_id": None, "id": "dir-1"}]
    assert [upload["filename"] for upload in client.upload_calls] == ["1001.md"]
    connector.close()


def test_page_size_defaults_to_ten_and_is_overrideable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")
    monkeypatch.delenv("ZENDESKTICKET_PAGE_SIZE", raising=False)
    default_connector = ZendeskTicketsConnector(state_dir=str(_make_state_dir(tmp_path, "default-page-size")))
    assert default_connector._page_size == 10
    default_connector.close()

    monkeypatch.setenv("ZENDESKTICKET_PAGE_SIZE", "25")
    custom_connector = ZendeskTicketsConnector(state_dir=str(_make_state_dir(tmp_path, "custom-page-size")))
    assert custom_connector._page_size == 25
    custom_connector.close()


def test_page_size_must_be_positive_integer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    monkeypatch.setenv("ZENDESKTICKET_PAGE_SIZE", "abc")
    with pytest.raises(ValueError, match="ZENDESKTICKET_PAGE_SIZE must be a positive integer"):
        ZendeskTicketsConnector(state_dir=str(_make_state_dir(tmp_path, "invalid-page-size")))

    monkeypatch.setenv("ZENDESKTICKET_PAGE_SIZE", "0")
    with pytest.raises(ValueError, match="ZENDESKTICKET_PAGE_SIZE must be a positive integer"):
        ZendeskTicketsConnector(state_dir=str(_make_state_dir(tmp_path, "zero-page-size")))


def test_external_attachment_downloads_without_authenticated_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "external-attachments")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(
                        1001,
                        "2024-01-02T03:04:05Z",
                        attachments=[_attachment("external.png")],
                    )
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1001: []},
    )
    external_calls: list[dict[str, object]] = []

    def fake_httpx_get(url: str, timeout: float) -> FakeBinaryResponse:
        external_calls.append({"url": url, "timeout": timeout})
        return FakeBinaryResponse(b"external-bytes")

    monkeypatch.setattr("oikb.connectors.zendesktickets.httpx.get", fake_httpx_get)

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["attachments/1001/1001-b5eb7d-external.png", "tickets/1001.md"]
    assert external_calls == [{"url": "https://attachments.example/external.png", "timeout": 30.0}]
    assert all(call["path"] != "https://attachments.example/external.png" for call in connector._http.calls)
    connector.close()


def test_zendesk_attachment_redirect_is_followed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "zendesk-attachment-redirect")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(
                        1001,
                        "2024-01-02T03:04:05Z",
                        attachments=[
                            _attachment(
                                "processing.pdf",
                                url="https://acme.zendesk.com/attachments/token/qb562ozvi78zu8u/?name=4510018376_processing.pdf",
                            )
                        ],
                    )
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1001: []},
    )

    original_get = connector._http.get

    def fake_get(path: str, params: dict[str, object] | None = None):
        if path.startswith("https://acme.zendesk.com/attachments/token/"):
            return FakeBinaryResponse(
                b"",
                status_code=302,
                headers={"location": "https://p27.zdusercontent.com/attachment/11400/qb562ozvi78zu8u?token=abc"},
            )
        if path.startswith("https://p27.zdusercontent.com/attachment/11400/qb562ozvi78zu8u"):
            return FakeBinaryResponse(b"redirected-bytes")
        return original_get(path, params)

    monkeypatch.setattr(connector._http, "get", fake_get)

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["attachments/1001/1001-6bd1d2-processing.pdf", "tickets/1001.md"]
    connector.close()


def test_missing_attachment_retries_then_skips_without_aborting_sync(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "missing-attachment-skip")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(
                        1001,
                        "2024-01-02T03:04:05Z",
                        attachments=[_attachment("missing.pdf", url="https://acme.zendesk.com/attachments/missing.pdf")],
                    )
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1001: []},
    )

    sleep_calls: list[float] = []
    original_get = connector._http.get
    calls = {"count": 0}

    def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    def fake_get(path: str, params: dict | None = None):
        if path == "https://acme.zendesk.com/attachments/missing.pdf":
            calls["count"] += 1
            return FakeBinaryResponse(b"", status_code=404)
        return original_get(path, params)

    monkeypatch.setattr("oikb.connectors.zendesktickets.time.sleep", fake_sleep)
    monkeypatch.setattr(connector._http, "get", fake_get)

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    assert calls["count"] == 4
    assert sleep_calls == [30, 60, 90]
    connector.close()


def test_rate_limited_ticket_fetch_retries_with_retry_after(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "rate-limit-retry")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "end_of_stream": True, "after_cursor": "cursor-1"}],
        comments={1001: []},
    )
    monkeypatch.setenv("ZENDESKTICKET_MAX_RETRIES", "2")
    monkeypatch.setenv("ZENDESKTICKET_BACKOFF_BASE_SECONDS", "0.1")
    monkeypatch.setenv("ZENDESKTICKET_BACKOFF_MAX_SECONDS", "1")
    connector._max_retries = 2
    connector._backoff_base_seconds = 0.1
    connector._backoff_max_seconds = 1.0

    original_get = connector._http.get
    sleep_calls: list[float] = []
    calls = {"count": 0}

    def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    def fake_get(path: str, params: dict | None = None):
        if path == "/incremental/tickets/cursor.json":
            calls["count"] += 1
            if calls["count"] == 1:
                return FakeResponse({}, status_code=429, headers={"retry-after": "0.25"})
        return original_get(path, params)

    monkeypatch.setattr("oikb.connectors.zendesktickets.time.sleep", fake_sleep)
    monkeypatch.setattr(connector._http, "get", fake_get)

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    assert sleep_calls == [0.25]
    connector.close()


def test_inaccessible_ticket_comments_404_skips_ticket_without_aborting_sync(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "inaccessible-ticket-skip")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T03:04:05Z"),
                    _ticket(1002, "2024-01-02T04:00:00Z"),
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1001: [_comment(501, "Accessible ticket comment.")], 1002: []},
    )
    connector._http = FakeHTTPClient(
        ticket_pages=connector._http._ticket_pages,
        comments={1001: [_comment(501, "Accessible ticket comment.")]},
        comment_status_codes={1002: [404]},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    connector.close()


def test_inaccessible_ticket_comments_5xx_retries_then_skips_without_aborting_sync(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "inaccessible-ticket-5xx-skip")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T03:04:05Z"),
                    _ticket(1002, "2024-01-02T04:00:00Z"),
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1001: [], 1002: []},
    )
    connector._max_retries = 2
    connector._backoff_base_seconds = 0.1
    connector._backoff_max_seconds = 1.0

    # ticket 1001 succeeds; ticket 1002 always returns 503
    original_http = connector._http
    connector._http = FakeHTTPClient(
        ticket_pages=original_http._ticket_pages,
        comments={1001: []},
        comment_status_codes={1002: [503, 503, 503]},
    )

    sleep_calls: list[float] = []
    monkeypatch.setattr("oikb.connectors.zendesktickets.time.sleep", lambda d: sleep_calls.append(d))

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    assert len(sleep_calls) == 2  # two retries before giving up
    connector.close()


def test_end_of_stream_true_stops_pagination_even_when_after_cursor_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """end_of_stream=true must terminate iteration even when after_cursor is set.

    The cursor-based incremental endpoint always returns an after_cursor.
    The only reliable signal that pagination is done is end_of_stream=true;
    the final after_cursor has no resume role. Without this guard the sync
    would keep requesting cursors forever.
    """
    state_dir = _make_state_dir(tmp_path, "end-of-stream")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_of_stream": True,
                "after_cursor": "cursor-1",
            }
        ],
        comments={1001: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    # Only one GET should have been made (no cursor follow-up after end_of_stream).
    ticket_calls = [c for c in connector._http.calls if "cursor.json" in (c["path"] or "")]
    assert len(ticket_calls) == 1
    connector.close()


def test_duplicate_after_cursor_stops_pagination(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A repeated after_cursor must not be followed a second time.

    Secondary safety guard: if end_of_stream never turns true but consecutive
    pages return the same after_cursor, following it would loop forever (the
    equal-timestamp fixed-point hazard generalized to cursors). The guard
    tracks seen cursors and breaks when one repeats.
    """
    state_dir = _make_state_dir(tmp_path, "stale-cursor")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_of_stream": False,
                "after_cursor": "same-cursor",
            },
            {
                "tickets": [],
                "end_of_stream": False,  # never reports done
                "after_cursor": "same-cursor",  # same cursor again — guard fires
            },
        ],
        comments={1001: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    # Exactly two cursor fetches: initial + one follow (then guard breaks).
    incremental_calls = [c for c in connector._http.calls if "cursor.json" in (c.get("path") or "")]
    assert len(incremental_calls) == 2
    connector.close()


# ── MAX_TICKETS_PER_RUN tests ──────────────────────────────────────────────

def test_max_tickets_per_run_stops_after_cap_and_saves_cursor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """ZENDESKTICKET_MAX_TICKETS_PER_RUN caps tickets per run, page-granular.

    Excluded tickets (tag/status filtered) do not count toward the cap.
    When the cap fires mid-page the connector finishes the current page —
    the resume point is the page's opaque after_cursor, so stopping mid-page
    would risk gaps (the next cursor resumes strictly after the page) and
    overshoot is bounded by per_page - 1. The next run resumes from that
    cursor via ?cursor=, never from start_time.
    """
    state_dir = _make_state_dir(tmp_path, "max-tickets-cap")
    cursor_path = state_dir / "resume_cursor.txt"
    monkeypatch.setenv("ZENDESKTICKET_MAX_TICKETS_PER_RUN", "2")
    # Three tickets on one page; cap of 2 fires on the second included ticket
    # but the page is finished, so 1003 is also processed this run.
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T01:00:00Z"),
                    _ticket(1002, "2024-01-02T02:00:00Z"),
                    _ticket(1003, "2024-01-02T03:00:00Z"),
                ],
                "end_of_stream": True,
                "after_cursor": "cursor-1",
            }
        ],
        comments={1001: [], 1002: [], 1003: []},
    )
    connector._max_tickets_per_run = 2

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md", "tickets/1002.md", "tickets/1003.md"]
    assert not (state_dir / "resume_checkpoint.txt").exists()
    # Sync completes (uploads succeeded); the resume cursor is the capped
    # page's after_cursor. The next run resumes via ?cursor=cursor-1.
    connector.mark_sync_complete()
    connector.close()
    assert cursor_path.read_text().strip() == "cursor-1"


def test_max_tickets_per_run_excluded_tickets_do_not_count_toward_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Tickets excluded by tag filter must not consume cap slots."""
    state_dir = _make_state_dir(tmp_path, "max-tickets-excluded")
    monkeypatch.setenv("ZENDESKTICKET_MAX_TICKETS_PER_RUN", "2")
    monkeypatch.setenv("ZENDESKTICKET_INCLUDETAGS", "ops")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T01:00:00Z", tags=["facilities"]),  # excluded
                    _ticket(1002, "2024-01-02T02:00:00Z", tags=["ops"]),
                    _ticket(1003, "2024-01-02T03:00:00Z", tags=["ops"]),
                    _ticket(1004, "2024-01-02T04:00:00Z", tags=["ops"]),
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1002: [], 1003: [], 1004: []},
    )
    connector._max_tickets_per_run = 2

    manifest = connector.build_manifest()

    # 1001 excluded (does not consume a cap slot), then 1002 and 1003 fill
    # the 2-slot cap; the page is finished so 1004 is also processed.
    assert [entry.display_path for entry in manifest] == ["tickets/1002.md", "tickets/1003.md", "tickets/1004.md"]
    connector.close()


def test_max_tickets_per_run_cap_on_equal_timestamp_page_still_advances_cursor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A cap firing on an all-equal-timestamp page must still advance the cursor.

    Live Zendesk data shows overlapping, non-monotonic incremental updated_at
    values, including pages where every record sits at the same timestamp —
    the old start_time design stalled on such pages (the #20 fixed point).
    The opaque after_cursor is immune: every page boundary yields a distinct
    cursor, so each cap-limited run resumes strictly past the processed page
    regardless of the updated_at values it carried.
    """
    state_dir = _make_state_dir(tmp_path, "max-tickets-equal-ts")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T02:00:00Z")
    cursor_path = state_dir / "resume_cursor.txt"
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                # Page 1: every ticket sits at the identical updated_at —
                # the exact pattern that stalled the start_time checkpoint.
                "tickets": [
                    _ticket(1001, "2024-01-02T02:00:00Z"),
                    _ticket(1002, "2024-01-02T02:00:00Z"),
                ],
                "end_of_stream": False,
                "after_cursor": "cursor-A",
            },
            {
                "tickets": [
                    _ticket(1003, "2024-01-02T02:00:00Z"),
                    _ticket(1004, "2024-01-02T02:00:00Z"),
                ],
                "end_of_stream": True,
                "after_cursor": "cursor-B",
            },
        ],
        comments={1001: [], 1002: [], 1003: [], 1004: []},
    )
    connector._max_tickets_per_run = 3

    manifest = connector.build_manifest()

    # Cap fires on page 2 after 1003, but the page is finished so 1004 is
    # also processed; the cursor advances to page 2's after_cursor even
    # though every ticket in the run shares one timestamp.
    assert [entry.display_path for entry in manifest] == ["tickets/1001.md", "tickets/1002.md", "tickets/1003.md", "tickets/1004.md"]
    connector.mark_sync_complete()
    assert cursor_path.read_text().strip() == "cursor-B"
    connector.close()


def test_malformed_after_cursor_fails_run_without_advancing_cursor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A malformed after_cursor must fail the run, not enable a fallback or loop.

    The opaque cursor is the only safe resume point; an empty-string cursor
    (or a missing cursor on a non-terminal page) previously crashed or
    silently looped the sync after tickets had been processed, leaving the
    resume point stuck. Validation now raises a normalized ValueError
    synchronously BEFORE any ticket is processed, and — because no page
    boundary is reached — no cursor file is written. The next run resumes
    from the last safe point.
    """
    state_dir = _make_state_dir(tmp_path, "malformed-after-cursor")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T02:00:00Z")

    # Variant 1: empty-string after_cursor.
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "after_cursor": "",
                "end_of_stream": True,
            }
        ],
        comments={1001: []},
    )
    with pytest.raises(ValueError, match="malformed after_cursor"):
        connector.build_manifest()
    connector.close()
    assert checkpoint_path.read_text().strip() == "2024-01-02T02:00:00Z"
    assert not (state_dir / "resume_cursor.txt").exists()

    # Variant 2: after_cursor key absent while end_of_stream is False — the
    # page is not terminal, so a missing cursor would silently loop; must fail.
    state_dir2 = _make_state_dir(tmp_path, "malformed-after-cursor-nonterminal")
    checkpoint_path2 = state_dir2 / "resume_checkpoint.txt"
    checkpoint_path2.write_text("2024-01-02T02:00:00Z")
    connector2 = _build_connector(
        monkeypatch,
        state_dir2,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "end_of_stream": False}],
        comments={1001: []},
    )
    with pytest.raises(ValueError, match="malformed after_cursor"):
        connector2.build_manifest()
    connector2.close()
    assert checkpoint_path2.read_text().strip() == "2024-01-02T02:00:00Z"
    assert not (state_dir2 / "resume_cursor.txt").exists()


def test_max_tickets_per_run_zero_disables_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Setting ZENDESKTICKET_MAX_TICKETS_PER_RUN=0 disables the cap entirely."""
    state_dir = _make_state_dir(tmp_path, "max-tickets-disabled")
    monkeypatch.setenv("ZENDESKTICKET_MAX_TICKETS_PER_RUN", "0")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T01:00:00Z"),
                    _ticket(1002, "2024-01-02T02:00:00Z"),
                    _ticket(1003, "2024-01-02T03:00:00Z"),
                ],
                "end_of_stream": True, "after_cursor": "cursor-1",
            }
        ],
        comments={1001: [], 1002: [], 1003: []},
    )
    assert connector._max_tickets_per_run is None

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md", "tickets/1002.md", "tickets/1003.md"]
    connector.close()


def test_max_tickets_per_run_cap_spans_multiple_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Cap is enforced across page boundaries; current page finishes after cap fires.

    The cap fires on 1003 (3rd ticket) mid-page-2; per the page-granular cap
    rule the rest of the page (1004) is still processed so the cursor can
    safely be that page's after_cursor.
    """
    state_dir = _make_state_dir(tmp_path, "max-tickets-multipage")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T01:00:00Z"),
                    _ticket(1002, "2024-01-02T02:00:00Z"),
                ],
                "end_of_stream": False,
                "after_cursor": "cursor-A",
            },
            {
                "tickets": [
                    _ticket(1003, "2024-01-02T03:00:00Z"),
                    _ticket(1004, "2024-01-02T04:00:00Z"),
                ],
                "end_of_stream": True,
                "after_cursor": "cursor-1",
            },
        ],
        comments={1001: [], 1002: [], 1003: [], 1004: []},
    )
    connector._max_tickets_per_run = 3

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md", "tickets/1002.md", "tickets/1003.md", "tickets/1004.md"]
    connector.close()


def test_cap_limited_run_resumes_via_cursor_param_not_start_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Equal-timestamp cap stall regression (Copilot review round 10).

    The old start_time design could hit a fixed point: if every record on the
    cap-limited page sat at the inclusive start boundary, end_time equaled
    checkpoint and no run ever advanced. With the cursor-based export the
    resume request must carry the page's opaque after_cursor via ?cursor=
    and MUST NOT pass start_time — starting the next run is immune to the
    tickets' updated_at values entirely.
    """
    state_dir = _make_state_dir(tmp_path, "cap-resume-cursor")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                # All tickets share one updated_at — the exact fixed-point
                # pattern from the live sync. The cap must still let the next
                # run resume past this page.
                "tickets": [
                    _ticket(1001, "2024-01-02T02:00:00Z"),
                    _ticket(1002, "2024-01-02T02:00:00Z"),
                    _ticket(1003, "2024-01-02T02:00:00Z"),
                ],
                "end_of_stream": False,
                "after_cursor": "cursor-A",
            }
        ],
        comments={1001: [], 1002: [], 1003: []},
    )
    connector._max_tickets_per_run = 2

    manifest = connector.build_manifest()

    # Page finishes after the cap; the page cursor is persisted.
    assert [entry.display_path for entry in manifest] == ["tickets/1001.md", "tickets/1002.md", "tickets/1003.md"]
    cursor_path = state_dir / "resume_cursor.txt"
    connector.mark_sync_complete()
    assert cursor_path.read_text().strip() == "cursor-A"
    connector.close()

    # Second run must resume via the cursor param — no start_time in request.
    connector2 = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1004, "2024-01-02T02:00:00Z")],
                "end_of_stream": True,
                "after_cursor": "cursor-B",
            }
        ],
        comments={1004: []},
    )
    connector2.build_manifest()
    connector2.close()

    fetch_calls = [c for c in connector2._http.calls if "cursor.json" in (c.get("path") or "")]
    assert len(fetch_calls) == 1
    params = fetch_calls[0].get("params") or {}
    assert params.get("cursor") == "cursor-A"
    assert "start_time" not in params
