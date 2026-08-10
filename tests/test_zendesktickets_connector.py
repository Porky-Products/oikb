from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from oikb.connectors import ManifestEntry
from oikb.connectors.zendesktickets import ZendeskTicketsConnector, parse_zendesktickets_source
from oikb.sync import run_sync


TEST_STATE_ROOT = ROOT / ".test-state" / "zendesktickets"


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeHTTPClient:
    def __init__(self, ticket_pages: list[dict], comments: dict[int, list[dict]] | None = None, attachments: dict[str, bytes] | None = None):
        self._ticket_pages = list(ticket_pages)
        self._comments = comments or {}
        self._attachments = attachments or {}
        self.calls: list[dict] = []
        self.is_closed = False

    def get(self, path: str, params: dict | None = None) -> FakeResponse:
        self.calls.append({"path": path, "params": params})
        if path == "/tickets.json":
            if not self._ticket_pages:
                raise AssertionError("No more ticket pages configured")
            return FakeResponse(self._ticket_pages.pop(0))
        if path.endswith("/comments.json"):
            ticket_id = int(path.split("/")[2])
            return FakeResponse({"comments": self._comments.get(ticket_id, [])})
        if path.startswith("https://attachments.example/"):
            name = path.rsplit("/", 1)[-1]
            return FakeBinaryResponse(self._attachments[name])
        raise AssertionError(f"Unexpected request: {path}")

    def close(self) -> None:
        self.is_closed = True


class FakeBinaryResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
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


def _make_state_dir(name: str) -> Path:
    state_dir = TEST_STATE_ROOT / name
    if state_dir.exists():
        shutil.rmtree(state_dir)
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


def test_build_manifest_uses_min_datetime_when_checkpoint_missing(monkeypatch: pytest.MonkeyPatch):
    state_dir = _make_state_dir("missing-checkpoint")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z", tags=["ops"])], "next_page": None}],
        comments={1001: []},
    )

    entries = connector.build_manifest()

    assert [entry.filename for entry in entries] == ["1001.md"]
    assert connector._http.calls[0]["params"]["start_time"] == 0
    connector.close()


def test_build_manifest_renders_comments_and_persists_state_after_success(monkeypatch: pytest.MonkeyPatch):
    state_dir = _make_state_dir("render-comments")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z", tags=["ops"])], "next_page": None}],
        comments={1001: [_comment(501, "Investigating the printer queue.")]},
    )

    manifest = connector.build_manifest()
    text = connector.read_file("", "1001.md").decode()

    assert manifest == [ManifestEntry(filename="1001.md", path="", checksum=manifest[0].checksum, size=len(text.encode("utf-8")))]
    assert "## Comments" in text
    assert "Investigating the printer queue." in text
    assert "Updated at: 2024-01-02T03:04:05Z" in text
    connector.mark_sync_complete()
    assert (state_dir / "resume_checkpoint.txt").read_text().strip() == "2024-01-02T03:04:05Z"
    connector.close()


def test_sync_failure_does_not_advance_checkpoint(monkeypatch: pytest.MonkeyPatch):
    state_dir = _make_state_dir("checkpoint-on-success")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "next_page": None}],
        comments={1001: []},
    )

    connector.build_manifest()

    assert not (state_dir / "resume_checkpoint.txt").exists()
    connector.close()


def test_sync_run_advances_checkpoint_only_after_upload_success(monkeypatch: pytest.MonkeyPatch):
    state_dir = _make_state_dir("run-sync-checkpoint")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "next_page": None}],
        comments={1001: []},
    )
    client = FakeClient()

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert result.added == 1
    assert (state_dir / "resume_checkpoint.txt").read_text().strip() == "2024-01-02T03:04:05Z"


def test_build_manifest_includes_previously_synced_unchanged_ticket(monkeypatch: pytest.MonkeyPatch):
    state_dir = _make_state_dir("carry-forward")
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
                                "path": "",
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
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "next_page": None}],
        comments={1002: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["1001.md", "1002.md"]
    connector.close()


def test_include_and_exclude_tags_filter_ticket_set(monkeypatch: pytest.MonkeyPatch):
    state_dir = _make_state_dir("tag-filter")
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
                "next_page": None,
            }
        ],
        comments={1001: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["1001.md"]
    connector.close()


def test_filtered_ticket_is_removed_from_kb_on_next_sync(monkeypatch: pytest.MonkeyPatch):
    state_dir = _make_state_dir("filtered-removal")
    existing_files = [
        {"path": "", "filename": "1001.md", "checksum": "old", "file_id": "file-1"},
        {"path": "1001", "filename": "1001-screenshot.png", "checksum": "old2", "file_id": "file-2"},
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
                            {"path": "", "filename": "1001.md", "checksum": "old", "size": 10},
                            {"path": "1001", "filename": "1001-screenshot.png", "checksum": "old2", "size": 4},
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
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z", tags=["facilities"])], "next_page": None}],
        comments={},
    )
    client = FakeClient(existing_files=existing_files)

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert client.cleanup_calls == [{"kb_id": "kb-1", "file_ids": ["file-1", "file-2"], "dir_ids": None}]
    assert result.deleted == 2


def test_attachments_are_added_when_enabled(monkeypatch: pytest.MonkeyPatch):
    state_dir = _make_state_dir("attachments-enabled")
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
                        attachments=[_attachment("error-log.txt")],
                    )
                ],
                "next_page": None,
            }
        ],
        comments={1001: [_comment(501, "See screenshot", attachments=[_attachment("screenshot.png")])]},
        attachments={"error-log.txt": b"log-bytes", "screenshot.png": b"image-bytes"},
    )

    manifest = connector.build_manifest()

    assert sorted(entry.display_path for entry in manifest) == [
        "1001.md",
        "1001/1001-error-log.txt",
        "1001/1001-screenshot.png",
    ]
    assert connector.read_file("1001", "1001-screenshot.png") == b"image-bytes"
    connector.close()


def test_disabling_attachments_removes_prior_attachment_files(monkeypatch: pytest.MonkeyPatch):
    state_dir = _make_state_dir("attachments-disabled")
    (state_dir / "manifest_state.json").write_text(
        json.dumps(
            {
                "checkpoint": "2024-01-01T00:00:00Z",
                "attachments_enabled": True,
                "ticket_files": {
                    "1001": {
                        "updated_at": "2024-01-01T00:00:00Z",
                        "entries": [
                            {"path": "", "filename": "1001.md", "checksum": "md", "size": 10},
                            {"path": "1001", "filename": "1001-screenshot.png", "checksum": "img", "size": 5},
                        ],
                    }
                },
            }
        )
    )
    existing_files = [
        {"path": "", "filename": "1001.md", "checksum": "md", "file_id": "file-md"},
        {"path": "1001", "filename": "1001-screenshot.png", "checksum": "img", "file_id": "file-img"},
    ]
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "false")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "next_page": None}],
        comments={1001: []},
    )
    client = FakeClient(existing_files=existing_files)

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert result.deleted == 1
    assert [upload["filename"] for upload in client.upload_calls] == ["1001.md"]
    assert client.cleanup_calls == [{"kb_id": "kb-1", "file_ids": ["file-img", "file-md"], "dir_ids": None}]


def test_page_size_defaults_to_ten_and_is_overrideable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")
    monkeypatch.delenv("ZENDESKTICKET_PAGE_SIZE", raising=False)
    default_connector = ZendeskTicketsConnector(state_dir=str(_make_state_dir("default-page-size")))
    assert default_connector._page_size == 10
    default_connector.close()

    monkeypatch.setenv("ZENDESKTICKET_PAGE_SIZE", "25")
    custom_connector = ZendeskTicketsConnector(state_dir=str(_make_state_dir("custom-page-size")))
    assert custom_connector._page_size == 25
    custom_connector.close()
