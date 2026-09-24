"""Repeated daemon syncs must stop uploading unchanged duplicate failures."""

import asyncio
import hashlib
from collections import Counter
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

from oikb import cli, daemon, kb_sync
from oikb.connectors import BaseConnector, ManifestEntry
from oikb.duplicate_failures import DuplicateFailureTracker


class Source(BaseConnector):
    def __init__(self, files, reads):
        self.files = files
        self.reads = reads

    def build_manifest(self):
        return [
            ManifestEntry(name, "", hashlib.sha256(data).hexdigest(), len(data))
            for name, data in self.files.items()
        ]

    def read_file(self, path, filename):
        self.reads.append(filename)
        return self.files[filename]


def rejection(status=400, detail="Duplicate content detected"):
    response = httpx.Response(
        status, json={"detail": detail},
        request=httpx.Request("POST", "https://webui.example/files"),
    )
    return httpx.HTTPStatusError("upload rejected", request=response.request, response=response)


@pytest.fixture()
def sync_case():
    files = {"bad.txt": b"old"}
    reads = []
    client = Mock(base_url="https://webui.example")
    client.sync_diff.side_effect = lambda kb, manifest: {
        "modified": [{**entry, "stale_file_id": f"old-{entry['filename']}"} for entry in manifest]
    }
    client.upload_file.side_effect = rejection()
    tracker = DuplicateFailureTracker()

    def run(**kwargs):
        return kb_sync.run_entries_sync(
            client, [{"source": "test", "kb-id": "kb"}],
            resolve_connector=lambda *a, **kw: Source(files, reads),
            duplicate_failures=tracker, quiet=True, **kwargs,
        )

    return files, reads, client, tracker, run


def test_blocks_after_three_runs_and_dry_run_does_not_reset(sync_case):
    files, reads, client, tracker, run = sync_case
    assert [run().duplicate_blocked for _ in range(3)] == [0, 0, 1]
    assert len(reads) == client.upload_file.call_count == 3
    blocked = run()
    assert blocked.duplicate_blocked == 1
    assert blocked.modified == blocked.added == blocked.duplicate_skipped == 0
    assert "blocked after 3" in blocked.warnings[0]
    assert len(reads) == client.upload_file.call_count == 3
    client.sync_cleanup.assert_not_called()

    # Even previewing a new version must not erase the old version's block.
    files["bad.txt"] = b"preview"
    run(dry_run=True)
    files["bad.txt"] = b"old"
    assert run().duplicate_blocked == 1
    assert client.upload_file.call_count == 3
    files["bad.txt"] = b"new"
    assert run().duplicate_blocked == 0
    assert client.upload_file.call_count == 4
    tracker.clear()
    assert run().duplicate_blocked == 0
    assert client.upload_file.call_count == 5


@pytest.mark.parametrize("interruption", [None, rejection(403, "Forbidden"), httpx.ReadTimeout("timeout")])
def test_success_or_other_failure_breaks_consecutive_streak(sync_case, interruption, monkeypatch):
    _, _, client, _, run = sync_case
    monkeypatch.setattr("oikb.sync.time.sleep", lambda _: None)
    run()
    run()
    client.upload_file.side_effect = interruption
    run()
    client.upload_file.side_effect = rejection()
    assert [run().duplicate_blocked for _ in range(3)] == [0, 0, 1]


@pytest.mark.parametrize("resolved", ["removed", "unmodified"])
def test_no_longer_pending_file_resets_count(sync_case, resolved):
    files, _, client, _, run = sync_case
    for _ in range(3):
        run()
    original_diff = client.sync_diff.side_effect
    if resolved == "removed":
        files.clear()
    else:
        client.sync_diff.side_effect = lambda *args: {"unmodified_count": 1}
    assert run().duplicate_blocked == 0
    files["bad.txt"] = b"old"
    client.sync_diff.side_effect = original_diff
    assert run().duplicate_blocked == 0
    assert client.upload_file.call_count == 4


def test_source_http_error_is_not_counted_as_upload_rejection(sync_case, monkeypatch):
    _, _, client, _, run = sync_case
    monkeypatch.setattr(Source, "read_file", Mock(side_effect=rejection()))
    for _ in range(4):
        result = run()
        assert result.errors
        assert result.duplicate_blocked == 0
    client.upload_file.assert_not_called()


def test_blocked_added_file_is_not_silently_deduplicated(sync_case, monkeypatch):
    files, reads, client, _, run = sync_case
    monkeypatch.setattr(Source, "content_addressed_checksums", True)
    client.sync_diff.side_effect = lambda kb, manifest: {"added": manifest}
    client.list_kb_files.return_value = []
    for _ in range(3):
        run()
    client.list_kb_files.return_value = [{"id": "other", "hash": hashlib.sha256(files["bad.txt"]).hexdigest()}]
    result = run()
    assert result.duplicate_blocked == 1
    assert result.warnings
    assert result.duplicate_skipped == result.added == 0
    assert len(reads) == client.upload_file.call_count == 3


@pytest.fixture()
def daemon_case(monkeypatch, sync_case):
    files, reads, client, _, _ = sync_case
    entry = {"name": "test", "source": "test", "kb-id": "kb", "concurrency": 4}
    monkeypatch.setattr(daemon, "_entries", [entry])
    monkeypatch.setattr(daemon, "_sync_locks", {})
    monkeypatch.setattr(daemon, "_duplicate_failures", {})
    monkeypatch.setattr(daemon, "_scheduler_state", {})
    monkeypatch.setattr(daemon, "_shutdown_event", None)
    monkeypatch.setattr(daemon, "_history", Mock())
    monkeypatch.setattr(daemon, "_send_notification", AsyncMock())
    monkeypatch.setattr(daemon, "record_sync", Mock())
    monkeypatch.setattr(cli, "_make_client", lambda **kw: client)
    monkeypatch.setattr(cli, "_resolve_connector", lambda *a, **kw: Source(files, reads))
    return files, reads, client, entry


@pytest.mark.asyncio
async def test_daemon_blocks_large_batch_keeps_healthy_uploads_and_warns(daemon_case):
    files, reads, client, entry = daemon_case
    files.clear()
    files.update({f"bad-{i}.txt": b"same" for i in range(103)})
    files["healthy.txt"] = b"new"

    def upload(**kwargs):
        if kwargs["filename"] != "healthy.txt":
            raise rejection()

    client.upload_file.side_effect = upload
    for expected_status in ["partial", "partial", "success", "success"]:
        await daemon._run_entry(entry)
        assert daemon._scheduler_state["test"]["status"] == expected_status
    counts = Counter(reads)
    assert counts["healthy.txt"] == 4
    assert all(counts[f"bad-{i}.txt"] == 3 for i in range(103))
    assert client.upload_file.call_count == 103 * 3 + 4
    assert all(call.args[1] == ["old-healthy.txt"] for call in client.sync_cleanup.call_args_list)
    state = daemon._scheduler_state["test"]
    assert state["duplicate_blocked"] == 103
    assert len(state["warnings"]) == 103
    assert state["errors"] == []
    assert daemon.record_sync.call_args.kwargs["status"] == "success"
    history = daemon._history.log.call_args.kwargs
    assert history["status"] == "success"
    assert history["error"] is None
    notification = daemon._send_notification.call_args.args[1]
    assert notification["status"] == "success"
    assert notification["duplicate_blocked"] == 103
    assert len(notification["warnings"]) == 103


@pytest.mark.asyncio
async def test_explicit_retry_endpoint_resets_counts_under_kb_lock(daemon_case, monkeypatch):
    _, _, client, entry = daemon_case
    for _ in range(3):
        await daemon._run_entry(entry)
    await daemon._run_entry(entry)
    assert client.upload_file.call_count == 3
    await daemon.trigger_sync("test", dry_run=True)
    await daemon._run_entry(entry)
    assert client.upload_file.call_count == 3
    with pytest.raises(HTTPException) as exc:
        await daemon.trigger_sync("test", dry_run=True, retry_blocked=True)
    assert exc.value.status_code == 400

    # Capture and await the endpoint's background task deterministically.
    tasks = []
    create_task = asyncio.create_task

    def capture(coro):
        task = create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(daemon.asyncio, "create_task", capture)
    lock = daemon._sync_locks["kb"]
    async with lock:
        await daemon.trigger_sync("test", retry_blocked=True)
        await asyncio.gather(*tasks)
    await daemon._run_entry(entry)
    assert client.upload_file.call_count == 3  # A skipped retry cannot clear the block.
    tasks.clear()
    await daemon.trigger_sync("test", retry_blocked=True)
    await asyncio.gather(*tasks)
    assert client.upload_file.call_count == 4
    assert daemon._scheduler_state["test"]["status"] == "partial"


@pytest.mark.asyncio
async def test_daemon_failure_state_is_scoped_by_server_and_kb(daemon_case):
    _, _, client, entry = daemon_case
    for _ in range(3):
        await daemon._run_entry(entry)
    client.base_url = "https://other.example"
    await daemon._run_entry(entry)
    assert daemon._scheduler_state["test"]["status"] == "partial"
    client.base_url = "https://webui.example"
    await daemon._run_entry({**entry, "kb-id": "other-kb"})
    assert daemon._scheduler_state["test"]["status"] == "partial"
    await daemon._run_entry(entry)
    assert daemon._scheduler_state["test"]["status"] == "success"
    assert client.upload_file.call_count == 5


def test_same_filename_and_checksum_in_other_destination_has_own_streak():
    tracker = DuplicateFailureTracker()
    client = Mock()
    client.sync_diff.side_effect = lambda kb, manifest: {
        "modified": [{**entry, "stale_file_id": entry["path"]} for entry in manifest],
        "directory_map": {"left": "left", "right": "right"},
    }
    failing = "left"

    def upload(**kwargs):
        if kwargs["directory_id"] == failing:
            raise rejection()

    client.upload_file.side_effect = upload

    def run():
        return kb_sync.run_entries_sync(
            client,
            [{"source": path, "kb-id": "kb", "target-path": path} for path in ("left", "right")],
            resolve_connector=lambda *a, **kw: Source({"same.txt": b"same"}, []),
            duplicate_failures=tracker, quiet=True, concurrency=2,
        )

    assert [run().duplicate_blocked for _ in range(3)] == [0, 0, 1]
    failing = "right"
    result = run()
    assert result.duplicate_blocked == 1  # Left remains blocked; right starts at one.
    assert len(result.errors) == 1
    assert len(result.warnings) == 1
    assert client.upload_file.call_count == 7
    assert tracker.blocked_keys() == {("left", "same.txt", hashlib.sha256(b"same").hexdigest())}


def test_standalone_sync_does_not_share_failure_counts(sync_case):
    files, reads, client, _, _ = sync_case
    for _ in range(4):
        result = kb_sync.run_entries_sync(
            client, [{"source": "test", "kb-id": "kb"}],
            resolve_connector=lambda *a, **kw: Source(files, reads), quiet=True,
        )
        assert result.errors and result.duplicate_blocked == 0
    assert client.upload_file.call_count == 4
