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
        if path == "/incremental/tickets.json" or "/incremental/tickets.json" in path:
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

    def list_kb_files(self, kb_id: str) -> list[dict]:
        # Mirror OikbClient.list_kb_files: file dicts with id (checked
        # against stale ids by the duplicate-upload guard),
        # meta.file_hash / hash.
        return [
            {
                "id": f["file_id"],
                "hash": f["checksum"],
                "meta": {"file_hash": f["checksum"]},
            }
            for f in self.existing_files
        ]

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
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z", tags=["ops"])],
                "end_time": 1704164645,
                "next_page": None,
            }
        ],
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
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z", tags=["ops"])],
                "end_time": 1704164645,
                "next_page": None,
            }
        ],
        comments={1001: [_comment(501, "Investigating the printer queue.")]},
    )

    manifest = connector.build_manifest()
    text = connector.read_file("tickets", "1001.md").decode()

    assert manifest == [ManifestEntry(filename="1001.md", path="tickets", checksum=manifest[0].checksum, size=len(text.encode("utf-8")))]
    assert "## Comments" in text
    assert "Investigating the printer queue." in text
    assert "Updated at: 2024-01-02T03:04:05Z" in text
    connector.mark_sync_complete()
    assert (state_dir / "resume_checkpoint.txt").read_text().strip() == "2024-01-02T03:04:05Z"
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
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_time": 1704164645,
                "next_page": "https://acme.zendesk.com/api/v2/incremental/tickets.json?start_time=1704164645",
            },
            {
                "tickets": [_ticket(1002, "2024-01-02T04:04:05Z")],
                "end_time": 1704168245,
                "next_page": None,
            },
        ],
        comments={1001: [], 1002: []},
    )

    connector.build_manifest()

    # Simulate sync failure: entries for 1001 (page-1 boundary) must already be
    # in manifest_state.json alongside the advanced checkpoint.
    saved_state = json.loads((state_dir / "manifest_state.json").read_text())
    assert "1001" in saved_state["ticket_files"]
    assert checkpoint_path.read_text().strip() == "2024-01-02T04:04:05Z"
    connector.close()


def test_aggressive_state_saved_on_non_advancing_end_time_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Later pages whose end_time does not advance the cursor still save state.

    The max-compare tolerance retains the cursor when a page reports an equal
    or lower end_time, but that page's manifest entries must still reach
    manifest_state.json — otherwise a sync failure resumes past records that
    exist only in memory.
    """
    state_dir = _make_state_dir(tmp_path, "aggressive-non-advancing")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T02:00:00Z")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_time": 1704168245,
                "next_page": "https://acme.zendesk.com/api/v2/incremental/tickets.json?start_time=1704168245",
            },
            {
                "tickets": [_ticket(1002, "2024-01-02T04:04:05Z")],
                "end_time": 1704168245,
                "next_page": None,
            },
        ],
        comments={1001: [], 1002: []},
    )

    connector.build_manifest()

    # Simulate sync failure: page 2's end_time (1704168245 = 04:04:05Z) did not
    # advance the cursor, but its ticket 1002 must still be persisted.
    saved_state = json.loads((state_dir / "manifest_state.json").read_text())
    assert "1002" in saved_state["ticket_files"]
    checkpoint_path_value = checkpoint_path.read_text().strip()
    assert checkpoint_path_value == "2024-01-02T04:04:05Z"
    connector.close()


def test_first_end_time_below_run_start_does_not_rewind_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A first end_time at/below the run-start checkpoint must not rewind it.

    The inclusive >= start_time boundary can serve a page whose end_time
    equals (or, for a stale state-file checkpoint, sits below) run start.
    Assigning it unconditionally would let mark_sync_complete persist an
    older cursor once the file exists — rewinding the next export.
    """
    state_dir = _make_state_dir(tmp_path, "end-time-below-run-start")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    # 2024-01-02T03:00:00Z run start; page reports 02:00:00Z end_time.
    checkpoint_path.write_text("2024-01-02T03:00:00Z")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T02:30:00Z")],
                "end_time": 1704160800,
                "next_page": None,
            },
        ],
        comments={1001: []},
    )

    connector.build_manifest()
    connector.mark_sync_complete()

    assert checkpoint_path.read_text().strip() == "2024-01-02T03:00:00Z"
    connector.close()


def test_cap_defers_processing_on_pages_after_an_equal_boundary_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Once the cap fires on an equal-boundary page, later pages must not
    be fully processed just to find where the cursor advances.

    Processing every ticket on every subsequent page (comment/attachment
    fetches included) until Zendesk's cursor moves past the checkpoint
    defeats the cap's purpose of bounding per-run ticket processing. Pages
    scanned after the cap has already fired are checked for end_time only;
    their tickets are deferred (neither seen nor excluded) and the
    checkpoint is left unchanged so a later run re-serves and processes
    them under a fresh cap.
    """
    state_dir = _make_state_dir(tmp_path, "cap-equal-boundary")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T03:00:00Z")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:00:00Z")],
                "end_time": 1704164400,
                "next_page": "https://acme.zendesk.com/api/v2/incremental/tickets.json?start_time=1704164400&page=2",
            },
            {
                "tickets": [_ticket(1002, "2024-01-02T03:00:00Z")],
                "end_time": 1704164460,
                "next_page": None,
            },
        ],
        comments={1001: [], 1002: []},
    )
    connector._max_tickets_per_run = 1

    manifest = connector.build_manifest()
    connector.mark_sync_complete()

    # Only 1001 (the page where the cap fired) is processed this run; 1002
    # is deferred and the checkpoint stays put (marked stalled) so it is
    # retried next run.
    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    assert checkpoint_path.read_text().strip() == "STALLED_EQUAL_TIMESTAMP:2024-01-02T03:00:00Z"
    connector.close()


def test_stall_retry_makes_cumulative_progress_through_equal_timestamp_zone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Consecutive capped runs must converge through a >cap equal-timestamp zone.

    A bulk import can share one generated timestamp across more tickets than
    ZENDESKTICKET_MAX_TICKETS_PER_RUN allows per run. The stall sentinel makes
    the retry loadable, but an absolute cap would re-process the same first
    `cap` tickets on every retry and never advance the checkpoint (livelock).
    Each retry must skip already-served tickets (stored state + unchanged
    updated_at) without burning cap budget or dropping them from the
    manifest, so progress through the zone is cumulative and a later run
    advances the checkpoint past the zone.
    """
    state_dir = _make_state_dir(tmp_path, "stall-retry-converges")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T03:00:00Z")
    zone_tickets = [_ticket(2000 + i, "2024-01-02T03:00:00Z") for i in range(20)]
    zone_page_1 = {
        "tickets": zone_tickets[0:10],
        "end_time": 1704164400,  # 2024-01-02T03:00:00Z == checkpoint
        "next_page": "https://acme.zendesk.com/api/v2/incremental/tickets.json?start_time=1704164400&page=2",
    }
    zone_page_2 = {
        "tickets": zone_tickets[10:20],
        "end_time": 1704164400,
        "next_page": "https://acme.zendesk.com/api/v2/incremental/tickets.json?start_time=1704164400&page=3",
    }
    after_zone_page = {
        "tickets": [_ticket(3000, "2024-01-02T05:00:00Z")],
        "end_time": 1704171600,  # 2024-01-02T05:00:00Z
        "next_page": None,
    }
    comments = {tid: [] for tid in range(2000, 2020)}
    comments[3000] = []
    all_ids = [f"tickets/{2000 + i}.md" for i in range(20)]

    for run in range(3):
        connector = _build_connector(
            monkeypatch,
            state_dir,
            pages=[dict(zone_page_1), dict(zone_page_2), dict(after_zone_page)],
            comments=comments,
        )
        connector._max_tickets_per_run = 10
        manifest = connector.build_manifest()
        connector.mark_sync_complete()
        ids = [entry.display_path for entry in manifest]
        if run == 0:
            # Cap fires inside the zone: first slice processed, stall.
            assert ids == all_ids[0:10]
            assert checkpoint_path.read_text().strip() == "STALLED_EQUAL_TIMESTAMP:2024-01-02T03:00:00Z"
        elif run == 1:
            # Retry skips already-served tickets and processes the next
            # slice; prior entries are carried forward in the manifest.
            assert ids == all_ids[0:20]
            assert checkpoint_path.read_text().strip() == "STALLED_EQUAL_TIMESTAMP:2024-01-02T03:00:00Z"
        else:
            # Zone exhausted: checkpoint advances past it, ticket 3000 syncs.
            assert ids == all_ids + ["tickets/3000.md"]
            assert checkpoint_path.read_text().strip() == "2024-01-02T05:00:00Z"
        connector.close()


def test_terminal_equal_boundary_writes_stall_sentinel_and_next_run_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """Stall sentinel must be retryable, not a permanent block.

    When time-based export cannot advance past capped records, the sentinel
    marks the condition on disk. A subsequent run must NOT raise (that
    would block every future run forever) — it logs a warning and retries
    from the stalled timestamp.
    """
    state_dir = _make_state_dir(tmp_path, "cap-terminal-equal-boundary")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T03:00:00Z")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:00:00Z")],
                "end_time": 1704164400,
                "end_of_stream": True,
                "next_page": None,
            }
        ],
        comments={1001: []},
    )
    connector._max_tickets_per_run = 1

    connector.build_manifest()
    connector.mark_sync_complete()
    connector.close()

    assert checkpoint_path.read_text().strip() == "STALLED_EQUAL_TIMESTAMP:2024-01-02T03:00:00Z"

    # Next run must retry from the stalled timestamp, not raise.
    connector2 = _build_connector(monkeypatch, state_dir, pages=[])
    state = connector2._load_state()
    with caplog.at_level("WARNING"):
        loaded = connector2._load_checkpoint(state)
    assert loaded.strftime("%Y-%m-%dT%H:%M:%SZ") == "2024-01-02T03:00:00Z"
    assert any("checkpoint_stalled" in rec.message for rec in caplog.records)
    assert all(rec.levelname != "ERROR" for rec in caplog.records)
    connector2.close()


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
        "end_time": 1704164645,
        "next_page": None,
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


def test_aggressive_state_is_saved_before_first_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """First aggressive checkpoint must persist page state before its cursor."""
    state_dir = _make_state_dir(tmp_path, "first-end-time-run-start")
    resume_path = state_dir / "resume_checkpoint.txt"
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
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_time": 1704164645,
                "next_page": None,
            }
        ],
        comments={1001: []},
    )

    class CrashAfterState(RuntimeError):
        pass

    def crash_before_checkpoint(value):
        raise CrashAfterState

    monkeypatch.setattr(connector, "_save_checkpoint", crash_before_checkpoint)
    with pytest.raises(CrashAfterState):
        connector.build_manifest()

    # State is durable while checkpoint remains at run start. Restart therefore
    # re-serves this page instead of skipping content absent from the manifest.
    saved_state = json.loads((state_dir / "manifest_state.json").read_text())
    assert saved_state["checkpoint"] == "2024-01-02T02:00:00Z"
    assert "1001" in saved_state["ticket_files"]
    assert not resume_path.exists()
    connector.close()


def test_sync_failure_does_not_advance_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "checkpoint-on-success")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_time": 1704164645,
                "next_page": None,
            }
        ],
        comments={1001: []},
    )

    connector._http._ticket_pages.append({"tickets": [], "next_page": None})
    connector.build_manifest()

    assert not (state_dir / "resume_checkpoint.txt").exists()
    connector.close()


def test_aggressive_checkpoint_keeps_run_cache_after_failure_for_resume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "aggressive-cache-preserve")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_time": 1704164645,  # 2024-01-02T03:04:05Z
                "next_page": None,
            }
        ],
        comments={1001: []},
    )

    connector.build_manifest()

    # Simulate failed sync: close() is always called (even on failure) by run_sync().
    # With aggressive checkpointing and a checkpoint file present, close() should NOT
    # delete .run-cache so the next run can resume.
    assert (state_dir / "resume_checkpoint.txt").exists()
    run_cache = state_dir / ".run-cache"
    assert run_cache.exists()
    sentinel = run_cache / "preserve.me"
    sentinel.write_text("keep")

    connector.close()

    # .run-cache must survive close() when aggressive checkpoint is active.
    assert sentinel.exists()


def test_aggressive_checkpoint_keeps_run_cache_with_state_only_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Crash recovery must retain cache when checkpoint exists only in state."""
    state_dir = _make_state_dir(tmp_path, "aggressive-state-only-cache")
    (state_dir / "manifest_state.json").write_text(
        json.dumps(
            {
                "checkpoint": "2024-01-02T03:04:05Z",
                "ticket_files": {},
                "attachments_enabled": False,
            }
        )
    )
    run_cache = state_dir / ".run-cache"
    run_cache.mkdir()
    sentinel = run_cache / "preserve.me"
    sentinel.write_text("keep")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "true")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [], "end_time": 1704164645, "next_page": None}],
    )

    connector.build_manifest()
    connector.close()

    assert sentinel.exists()

    # A new connector instance picks up the preserved cache and resumes from checkpoint.
    connector2 = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_time": 1704164645,
                "next_page": None,
            }
        ],
        comments={1001: []},
    )
    connector2.build_manifest()
    assert connector2._http.calls[0]["params"]["start_time"] == 1704164645
    assert sentinel.exists()
    connector2.close()


def test_sync_run_advances_checkpoint_after_completed_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "run-sync-checkpoint")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_time": 1704164645,
                "next_page": None,
            }
        ],
        comments={1001: []},
    )
    client = FakeClient()

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert result.added == 1
    assert (state_dir / "resume_checkpoint.txt").read_text().strip() == "2024-01-02T03:04:05Z"


def test_sync_dry_run_does_not_advance_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "dry-run-no-checkpoint")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "false")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_time": 1704164645,
                "next_page": None,
            }
        ],
        comments={1001: []},
    )
    client = FakeClient()

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True, dry_run=True)

    assert result.added == 1
    assert not (state_dir / "resume_checkpoint.txt").exists()


def test_sync_exception_does_not_advance_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "exception-no-checkpoint")
    monkeypatch.setenv("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "false")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "next_page": None}],
        comments={1001: []},
    )

    class FailingClient(FakeClient):
        def sync_diff(self, kb_id: str, manifest: list[dict]) -> dict:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_sync(client=FailingClient(), connector=connector, kb_id="kb-1", quiet=True)

    assert not (state_dir / "resume_checkpoint.txt").exists()


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
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_time": 1704164645,
                "next_page": None,
            }
        ],
        comments={1001: []},
    )

    class UploadFailingClient(FakeClient):
        def upload_file(self, file_content: bytes, filename: str, kb_id: str, file_hash: str, directory_id: str | None = None) -> dict:
            raise httpx.HTTPStatusError("500", request=httpx.Request("POST", "https://example.com"), response=httpx.Response(500))

    result = run_sync(client=UploadFailingClient(), connector=connector, kb_id="kb-1", quiet=True)

    assert result.errors  # upload failed
    # Checkpoint must still have advanced.
    assert (state_dir / "resume_checkpoint.txt").read_text().strip() == "2024-01-02T03:04:05Z"


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
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "next_page": None}],
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
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "next_page": None}],
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
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "next_page": None}],
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
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "next_page": None}],
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
        pages=[{"tickets": [_ticket(1002, "2024-01-02T03:04:05Z")], "next_page": None}],
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
                "next_page": None,
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
                "next_page": None,
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
                "next_page": None,
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
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z", tags=["facilities"])], "next_page": None}],
        comments={},
    )
    client = FakeClient(existing_files=existing_files)

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert client.cleanup_calls == [{"kb_id": "kb-1", "file_ids": ["file-1", "file-2"], "dir_ids": None}]
    assert result.deleted == 2


def test_attachments_are_added_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "attachments-enabled")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENT_ALLOWED_EXTENSIONS", "txt,png")
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
                "next_page": None,
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
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "next_page": None}],
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
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENT_ALLOWED_EXTENSIONS", "png")
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
                "next_page": None,
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
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "next_page": None}],
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
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENT_ALLOWED_EXTENSIONS", "png")
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
                "next_page": None,
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


def test_default_attachment_allowlist_blocks_non_matching_extensions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "default-allowlist")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    monkeypatch.delenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENT_ALLOWED_EXTENSIONS", raising=False)
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
                "next_page": None,
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
        attachments={"error-log.txt": b"log-bytes"},
    )

    manifest = connector.build_manifest()

    assert sorted(entry.display_path for entry in manifest) == [
        "attachments/1001/1001-91561f-error-log.txt",
        "tickets/1001.md",
    ]
    # The blocked png must never be fetched.
    assert all("screenshot.png" not in call["path"] for call in connector._http.calls)
    connector.close()


def test_custom_attachment_allowed_extensions_enable_non_default_types(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "custom-allowlist")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENT_ALLOWED_EXTENSIONS", "PNG, .jpg")
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
                            _attachment("screenshot.png", url="https://acme.zendesk.com/attachments/screenshot.png"),
                            _attachment("photo.jpg", url="https://acme.zendesk.com/attachments/photo.jpg"),
                            _attachment("notes.docx", url="https://acme.zendesk.com/attachments/notes.docx"),
                        ],
                    )
                ],
                "next_page": None,
            }
        ],
        comments={1001: []},
        attachments={"screenshot.png": b"png-bytes", "photo.jpg": b"jpg-bytes"},
    )

    manifest = connector.build_manifest()

    extensions = {entry.filename.rsplit("-", 1)[-1].rsplit(".", 1)[-1] for entry in manifest if entry.path == "attachments/1001"}
    assert extensions == {"png", "jpg"}  # docx blocked by the custom list
    connector.close()


def test_empty_attachment_allowed_extensions_allows_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "allowlist-empty")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENT_ALLOWED_EXTENSIONS", "")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(
                        1001,
                        "2024-01-02T03:04:05Z",
                        attachments=[_attachment("screenshot.png", url="https://acme.zendesk.com/attachments/screenshot.png")],
                    )
                ],
                "next_page": None,
            }
        ],
        comments={1001: []},
        attachments={"screenshot.png": b"image-bytes"},
    )
    client = FakeClient(existing_files=[])

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert [entry for entry in [upload["filename"] for upload in client.upload_calls] if entry.endswith("screenshot.png")]
    state = json.loads((state_dir / "manifest_state.json").read_text())
    assert state["attachment_extensions"] is None
    connector.close()


def test_extensionless_attachment_blocked_when_allowlist_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "allowlist-no-extension")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    monkeypatch.delenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENT_ALLOWED_EXTENSIONS", raising=False)
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(
                        1001,
                        "2024-01-02T03:04:05Z",
                        attachments=[_attachment("README", url="https://acme.zendesk.com/attachments/README")],
                    )
                ],
                "next_page": None,
            }
        ],
        comments={1001: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    assert all("README" not in call["path"] for call in connector._http.calls)
    connector.close()


def test_tightened_allowlist_removes_prior_non_matching_attachments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A prior run (state predating the allowlist) synced a png; the default
    allowlist now blocks png, so the next run drops it from the KB."""
    state_dir = _make_state_dir(tmp_path, "allowlist-tightened")
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
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    monkeypatch.delenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENT_ALLOWED_EXTENSIONS", raising=False)
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [], "next_page": None}],
        comments={},
    )
    client = FakeClient(existing_files=existing_files)

    result = run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    assert result.deleted == 1
    assert client.cleanup_calls == [{"kb_id": "kb-1", "file_ids": ["file-img"], "dir_ids": None}]
    state = json.loads((state_dir / "manifest_state.json").read_text())
    filenames = [entry["filename"] for entry in state["ticket_files"]["1001"]["entries"]]
    assert filenames == ["1001.md"]
    connector.close()


def test_attachment_extensions_persisted_in_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_dir = _make_state_dir(tmp_path, "allowlist-persisted")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENT_ALLOWED_EXTENSIONS", "txt, pdf")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "next_page": None}],
        comments={1001: []},
    )
    client = FakeClient(existing_files=[])

    run_sync(client=client, connector=connector, kb_id="kb-1", quiet=True)

    state = json.loads((state_dir / "manifest_state.json").read_text())
    assert state["attachment_extensions"] == ["pdf", "txt"]
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
                "next_page": None,
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
                "next_page": None,
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
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "next_page": None}],
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
        if path == "/incremental/tickets.json":
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
                "next_page": None,
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
                "next_page": None,
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


def test_end_of_stream_true_stops_pagination_even_when_next_page_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """end_of_stream=true must terminate iteration even when next_page is set.

    Zendesk always returns a next_page URL on the incremental endpoint, even
    after all results have been delivered.  The only reliable signal that
    pagination is done is end_of_stream=true.  Without this guard the sync
    re-requests the same start_time forever.
    """
    state_dir = _make_state_dir(tmp_path, "end-of-stream")
    next_page_url = "https://acme.zendesk.com/api/v2/incremental/tickets.json?per_page=100&start_time=1786648901"
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "next_page": next_page_url,
                "end_of_stream": True,
            }
        ],
        comments={1001: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    # Only one GET should have been made (no follow-up on next_page).
    ticket_calls = [c for c in connector._http.calls if "incremental/tickets.json" in (c["path"] or "")]
    assert len(ticket_calls) == 1
    connector.close()


def test_stale_next_page_url_stops_pagination(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A repeated next_page URL (same cursor) must not be followed a second time.

    This is a secondary safety guard: if end_of_stream is missing or False but
    next_page resolves back to the same URL on consecutive pages, following it
    would loop forever.  The guard detects this by tracking seen URLs and
    breaking when the same next_page appears again.
    """
    state_dir = _make_state_dir(tmp_path, "stale-next-page")
    same_url = "https://acme.zendesk.com/api/v2/incremental/tickets.json?per_page=100&start_time=1786648901"
    # Two pages: both return the same next_page URL, simulating a stuck cursor.
    # The connector should follow it once (first page → second page) and then
    # stop when it would loop back to the same URL again.
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "next_page": same_url,
                "end_of_stream": False,
            },
            {
                "tickets": [],
                "next_page": same_url,  # same URL again — guard must fire here
                "end_of_stream": False,
            },
        ],
        comments={1001: []},
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md"]
    # Exactly two incremental ticket fetches: initial + one follow (then stop).
    incremental_calls = [c for c in connector._http.calls if "incremental/tickets.json" in (c.get("path") or "")]
    assert len(incremental_calls) == 2
    connector.close()


# ── MAX_TICKETS_PER_RUN tests ──────────────────────────────────────────────

def test_max_tickets_per_run_stops_after_cap_and_saves_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """ZENDESKTICKET_MAX_TICKETS_PER_RUN caps tickets per run, page-granular.

    Excluded tickets (tag/status filtered) do not count toward the cap.
    When the cap fires mid-page the connector finishes the current page —
    the checkpoint is the page's end_time (Zendesk's own forward cursor,
    = generated_timestamp of the page's last item), so stopping mid-page
    would risk gaps (start_time is compared against generated_timestamp,
    not updated_at) and overshoot is bounded by per_page - 1. The next run
    resumes from end_time; same-timestamp duplicates at the boundary are
    re-served by design.
    """
    state_dir = _make_state_dir(tmp_path, "max-tickets-cap")
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
                # end_time is generated_timestamp of the last page item.
                "end_time": 1704164400,
                "next_page": None,
            }
        ],
        comments={1001: [], 1002: [], 1003: []},
    )
    connector._max_tickets_per_run = 2

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md", "tickets/1002.md", "tickets/1003.md"]
    # Checkpoint is the page's end_time — Zendesk's own resume cursor.
    connector.mark_sync_complete()
    checkpoint_text = (state_dir / "resume_checkpoint.txt").read_text().strip()
    assert checkpoint_text == "2024-01-02T03:00:00Z"
    connector.close()


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
                "next_page": None,
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


def test_max_tickets_per_run_cap_on_out_of_order_page_does_not_stall_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A cap firing on an out-of-order page must not stall or regress the checkpoint.

    Live Zendesk data shows overlapping, non-monotonic incremental updated_at
    values: a later page's max updated_at can be earlier than an earlier
    page's — or equal to the incoming checkpoint. The checkpoint uses the
    page's end_time (Zendesk generated_timestamp cursor) when present, recovers
    the equivalent start_time from next_page when needed, and never advances
    from updated_at, so out-of-order ticket timestamps cannot create gaps.
    """
    state_dir = _make_state_dir(tmp_path, "max-tickets-out-of-order")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T02:00:00Z")
    next_page_url = "https://acme.zendesk.com/api/v2/incremental/tickets.json?per_page=2&start_time=1000"
    # end_time epochs: page 1 -> 2024-01-02T03:10:00Z (1704165000) after its
    # final ticket (1002, generated at 03:10), page 2 -> earlier — later page
    # has EARLIER end_time, mirroring out-of-order observed live behavior.
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                # Page 1 max updated_at (04:00) is later than page 2 max (02:00).
                "tickets": [
                    _ticket(1001, "2024-01-02T03:00:00Z"),
                    _ticket(1002, "2024-01-02T04:00:00Z"),
                ],
                "end_time": 1704165000,
                "next_page": next_page_url,
            },
            {
                # Page 2 max updated_at equals the incoming checkpoint — the
                # exact fixed-point pattern observed in production. With no
                # cursor on this terminal page, updated_at must not override
                # the authoritative page-1 end_time.
                "tickets": [
                    _ticket(1003, "2024-01-02T01:00:00Z"),
                    _ticket(1004, "2024-01-02T02:00:00Z"),
                ],
                "next_page": None,
            },
        ],
        comments={1001: [], 1002: [], 1003: [], 1004: []},
    )
    connector._max_tickets_per_run = 3

    manifest = connector.build_manifest()

    # Cap fires on page 2 after 1003, but the page is finished so 1004 is
    # also processed; the checkpoint keeps the max (page 1's end_time).
    assert [entry.display_path for entry in manifest] == ["tickets/1001.md", "tickets/1002.md", "tickets/1003.md", "tickets/1004.md"]
    assert connector._pending_checkpoint is not None and connector._pending_checkpoint.strftime("%Y-%m-%dT%H:%M:%SZ") == "2024-01-02T03:10:00Z"
    connector.mark_sync_complete()
    assert checkpoint_path.read_text().strip() == "2024-01-02T03:10:00Z"
    connector.close()


def test_legacy_page_updated_at_does_not_advance_past_end_time_cursor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A page without end_time must not push the checkpoint beyond an established end_time cursor.

    updated_at does not bound generated_timestamp: a legacy/degraded page
    whose max updated_at exceeds Zendesk's own end_time cursor could otherwise
    advance the checkpoint past records that were never served, and the
    no-regress guard would lock it in — the data-loss mode this change
    eliminates. Pages WITH end_time never fall back to updated_at; a terminal
    page without either cursor cannot advance the checkpoint.
    """
    state_dir = _make_state_dir(tmp_path, "legacy-page-no-override")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T02:00:00Z")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T03:00:00Z"),
                ],
                "end_time": 1704164400,  # 2024-01-02T03:00:00Z
                "next_page": "https://acme.zendesk.com/api/v2/incremental/tickets.json?per_page=1&start_time=1704164400",
            },
            {
                # Legacy payload: no end_time, but max updated_at (05:00)
                # beyond the end_time cursor (03:00).
                "tickets": [
                    _ticket(1002, "2024-01-02T04:00:00Z"),
                    _ticket(1003, "2024-01-02T05:00:00Z"),
                ],
                "next_page": None,
            },
        ],
        comments={1001: [], 1002: [], 1003: []},
    )

    connector.build_manifest()

    assert connector._pending_checkpoint is not None and connector._pending_checkpoint.strftime("%Y-%m-%dT%H:%M:%SZ") == "2024-01-02T03:00:00Z"
    connector.mark_sync_complete()
    assert checkpoint_path.read_text().strip() == "2024-01-02T03:00:00Z"
    connector.close()


@pytest.mark.parametrize("end_time", [None, True, 1704164645.9, "1704164645", 10**30])
def test_malformed_end_time_fails_run_without_advancing_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, end_time
):
    """A present-but-malformed end_time must fail without advancing.

    Strict validation rejects null, booleans, fractional numbers, numeric
    strings, and out-of-range epochs instead of coercing them into cursors.
    """
    state_dir = _make_state_dir(tmp_path, f"malformed-end-time-{end_time!r}")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T02:00:00Z")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [_ticket(1001, "2024-01-02T03:04:05Z")],
                "end_time": end_time,
                "next_page": None,
            }
        ],
        comments={1001: []},
    )

    with pytest.raises(ValueError, match="malformed end_time"):
        connector.build_manifest()

    connector.close()
    assert checkpoint_path.read_text().strip() == "2024-01-02T02:00:00Z"

def test_terminal_page_without_cursor_retains_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Missing terminal cursor may repeat tickets but must never skip them."""
    state_dir2 = _make_state_dir(tmp_path, "missing-terminal-cursor")
    checkpoint_path2 = state_dir2 / "resume_checkpoint.txt"
    checkpoint_path2.write_text("2024-01-02T02:00:00Z")
    connector2 = _build_connector(
        monkeypatch,
        state_dir2,
        pages=[{"tickets": [_ticket(1001, "2024-01-02T03:04:05Z")], "next_page": None}],
        comments={1001: []},
    )
    connector2.build_manifest()
    connector2.mark_sync_complete()
    assert checkpoint_path2.read_text().strip() == "2024-01-02T02:00:00Z"
    connector2.close()


def test_missing_end_time_uses_next_page_start_time_not_updated_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """next_page start_time is authoritative when end_time is absent.

    The page's updated_at is deliberately later than its generated-timestamp
    cursor. Resuming from updated_at would skip tickets; next_page start_time
    provides the same safe cursor Zendesk normally returns as end_time.
    """
    state_dir = _make_state_dir(tmp_path, "next-page-cursor")
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T02:00:00Z")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1002, "2024-01-02T05:00:00Z"),
                ],
                "next_page": "https://acme.zendesk.com/api/v2/incremental/tickets.json?per_page=1&start_time=1704164400",
            },
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T03:00:00Z"),
                ],
                "end_time": 1704164400,  # 2024-01-02T03:00:00Z
                "next_page": None,
            },
        ],
        comments={1001: [], 1002: []},
    )

    connector.build_manifest()

    assert connector._pending_checkpoint is not None and connector._pending_checkpoint.strftime("%Y-%m-%dT%H:%M:%SZ") == "2024-01-02T03:00:00Z"
    connector.mark_sync_complete()
    assert checkpoint_path.read_text().strip() == "2024-01-02T03:00:00Z"
    connector.close()


def test_save_checkpoint_never_regresses_existing_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """_save_checkpoint must refuse to persist a value older than the existing one."""
    state_dir = _make_state_dir(tmp_path, "checkpoint-no-regress")
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[{"tickets": [], "next_page": None}],
    )
    checkpoint_path = state_dir / "resume_checkpoint.txt"
    checkpoint_path.write_text("2024-01-02T04:00:00Z")

    from datetime import UTC, datetime

    connector._save_checkpoint(datetime(2024, 1, 2, 3, 0, 0, tzinfo=UTC))
    assert checkpoint_path.read_text().strip() == "2024-01-02T04:00:00Z"

    newer = datetime(2024, 1, 2, 5, 0, 0, tzinfo=UTC)
    connector._save_checkpoint(newer)
    assert checkpoint_path.read_text().strip() == "2024-01-02T05:00:00Z"
    connector.close()


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
                "next_page": None,
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
    rule the rest of the page (1004) is still processed so the checkpoint can
    safely be that page's end_time.
    """
    state_dir = _make_state_dir(tmp_path, "max-tickets-multipage")
    next_page_url = "https://acme.zendesk.com/api/v2/incremental/tickets.json?per_page=2&start_time=1000"
    connector = _build_connector(
        monkeypatch,
        state_dir,
        pages=[
            {
                "tickets": [
                    _ticket(1001, "2024-01-02T01:00:00Z"),
                    _ticket(1002, "2024-01-02T02:00:00Z"),
                ],
                "next_page": next_page_url,
            },
            {
                "tickets": [
                    _ticket(1003, "2024-01-02T03:00:00Z"),
                    _ticket(1004, "2024-01-02T04:00:00Z"),
                ],
                "next_page": None,
            },
        ],
        comments={1001: [], 1002: [], 1003: [], 1004: []},
    )
    connector._max_tickets_per_run = 3

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["tickets/1001.md", "tickets/1002.md", "tickets/1003.md", "tickets/1004.md"]
    connector.close()
