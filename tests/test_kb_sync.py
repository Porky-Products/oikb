from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest
from click.testing import CliRunner

from oikb import kb_sync
from oikb.connectors import BaseConnector, ManifestEntry, SourceFileUnavailable
from oikb.sync import SyncCancelled, SyncResult


class Source(BaseConnector):
    def __init__(self, content, fail=False):
        self.content = content
        self.fail = fail
        self.closed = False

    def build_manifest(self):
        if self.fail:
            raise RuntimeError("scan failed")
        return [ManifestEntry(name, "", str(len(data)), len(data)) for name, data in self.content.items()]

    def read_file(self, path, filename):
        assert path == ""  # Destination prefixes must not leak into source reads.
        return self.content[filename]

    def close(self):
        self.closed = True


def test_combined_manifest_filters_routing_and_auth():
    sources = [Source({"readme.txt": b"first", "skip.bin": b"ignored"}), Source({"readme.txt": b"second"})]
    resolver = Mock(side_effect=sources)
    client = Mock()
    client.sync_diff.side_effect = lambda kb, manifest: {"added": manifest}
    entries = [
        {"source": "confluence:ENG", "kb-id": "kb", "filter": {"include": ["*.txt"]}, "auth": {"token": "one"}},
        {"source": "confluence:HR", "kb-id": "kb", "auth": {"token": "two"}},
    ]
    result = kb_sync.run_entries_sync(client, entries, resolve_connector=resolver, quiet=True)
    assert result.added == 2
    assert [e["path"] for e in client.sync_diff.call_args.args[1]] == ["ENG", "HR"]
    assert [call.kwargs["file_content"] for call in client.upload_file.call_args_list] == [b"first", b"second"]
    assert [call.kwargs["auth"] for call in resolver.call_args_list] == [{"token": "one"}, {"token": "two"}]
    client.sync_diff.assert_called_once()
    client.sync_cleanup.assert_not_called()
    assert all(source.closed for source in sources)


@pytest.mark.parametrize("failure", ["scan", "resolve", "duplicate", "filter"])
def test_failure_prevents_any_kb_mutation_and_closes_sources(failure):
    first = Source({"a.txt": b"a"})
    second = Source({"a.txt" if failure == "duplicate" else "b.txt": b"b"}, fail=failure == "scan")
    resolver = Mock(side_effect=[first, RuntimeError("resolve failed") if failure == "resolve" else second])
    entries = [{"source": "one", "kb-id": "kb"}, {"source": "two", "kb-id": "kb"}]
    if failure == "filter":
        entries[1]["filter"] = {"max-size": "bad"}
    client = Mock()
    with pytest.raises((ValueError, RuntimeError)):
        kb_sync.run_entries_sync(client, entries, resolve_connector=resolver, quiet=True)
    assert not client.mock_calls
    assert first.closed
    if failure not in {"resolve", "filter"}:
        assert second.closed


def test_single_source_keeps_paths_and_target_path_is_optional():
    for extra, expected in [({}, ""), ({"target-path": "docs/api"}, "docs/api")]:
        client = Mock()
        client.sync_diff.return_value = {}
        source = Source({"a.txt": b"a"})
        kb_sync.run_entries_sync(client, [{"source": "confluence:ENG", "kb-id": "kb", **extra}], resolve_connector=lambda *a, **kw: source, quiet=True)
        assert client.sync_diff.call_args.args[1][0]["path"] == expected
        assert source.closed


@pytest.mark.parametrize("prefix", ["/absolute", "../parent", "a/../b", "a\\b", "a//b"])
def test_invalid_target_path_fails_before_scan(prefix):
    resolver = Mock()
    with pytest.raises(ValueError, match="target-path"):
        kb_sync.run_entries_sync(Mock(), [{"source": "one", "kb-id": "kb", "target-path": prefix}], resolve_connector=resolver)
    resolver.assert_not_called()


def test_group_validation_and_cancellation():
    with pytest.raises(ValueError, match="source and kb-id"):
        kb_sync.group_entries_by_kb([{"source": "one"}])
    with pytest.raises(ValueError, match="same url and token"):
        kb_sync.group_entries_by_kb([{"source": "one", "kb-id": "kb", "url": "a"}, {"source": "two", "kb-id": "kb", "url": "b"}])
    client, resolver = Mock(), Mock()
    with pytest.raises(SyncCancelled):
        kb_sync.run_entries_sync(client, [{"source": "one", "kb-id": "kb"}], resolve_connector=resolver, cancel_requested=lambda: True)
    assert not client.mock_calls and not resolver.mock_calls


def test_cli_name_selects_entire_kb_group_and_closes_client(monkeypatch):
    import oikb.cli as cli
    entries = [{"name": "one", "source": "one", "kb-id": "kb"}, {"source": "two", "kb-id": "kb"}, {"source": "other", "kb-id": "other"}]
    monkeypatch.setattr(cli, "_load_oikb_yaml", lambda: entries)
    client = Mock()
    monkeypatch.setattr(cli, "_make_client", lambda *a: client)
    sync = Mock(return_value=SyncResult())
    monkeypatch.setattr(kb_sync, "run_entries_sync", sync)
    result = CliRunner().invoke(cli.cli, ["sync", "--name", "one", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert sync.call_args.args[1] == entries[:2]
    assert sync.call_args.kwargs["dry_run"] is True
    client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["webhook", "alias", "kb"])
async def test_daemon_triggers_include_sibling_sources(monkeypatch, trigger):
    import oikb.cli as cli
    import oikb.daemon as daemon
    entries = [{"name": "one", "source": "one", "kb-id": "kb"}, {"source": "two", "kb-id": "kb"}]
    monkeypatch.setattr(daemon, "_entries", entries)
    monkeypatch.setattr(daemon, "_sync_locks", {})
    monkeypatch.setattr(daemon, "_scheduler_state", {})
    monkeypatch.setattr(daemon, "_shutdown_event", None)
    monkeypatch.setattr(daemon, "_history", None)
    monkeypatch.setattr(daemon, "_send_notification", AsyncMock())
    monkeypatch.setattr(daemon, "record_sync", Mock())
    client = Mock()
    monkeypatch.setattr(cli, "_make_client", lambda **kw: client)
    sync = Mock(return_value=SyncResult(added=2))
    monkeypatch.setattr(kb_sync, "run_entries_sync", sync)
    if trigger == "webhook":
        await daemon._run_entry(entries[0])  # The same callback registered with webhooks.
        assert all(daemon._scheduler_state[s]["files_added"] == 2 for s in ("one", "two"))
    else:
        result = await daemon.trigger_sync("one" if trigger == "alias" else "kb", dry_run=True)
        assert result["result"]["added"] == 2
        assert daemon._scheduler_state == {}
    assert sync.call_args.args[1] == entries
    client.close.assert_called_once()


def test_upload_error_includes_server_detail():
    client = Mock()
    client.sync_diff.side_effect = lambda kb, manifest: {"added": manifest}
    response = httpx.Response(400, json={"detail": "extraction failed"}, request=httpx.Request("POST", "https://webui.example/files"))
    client.upload_file.side_effect = httpx.HTTPStatusError("Bad Request", request=response.request, response=response)
    result = kb_sync.run_entries_sync(client, [{"source": "one", "kb-id": "kb"}], resolve_connector=lambda *a, **kw: Source({"a.txt": b"a"}), quiet=True)
    assert "extraction failed" in result.errors[0]


def test_combined_connector_exposes_checksums_only_when_every_child_does():
    providing = Mock()
    providing.content_addressed_checksums = True
    lacking = Mock()
    lacking.content_addressed_checksums = False
    assert kb_sync._CombinedConnector([], {}, [providing]).content_addressed_checksums is True
    assert kb_sync._CombinedConnector([], {}, [providing, lacking]).content_addressed_checksums is False


def test_combined_connector_mark_sync_complete_forwards_once_per_child():
    first, second = Mock(), Mock()
    plain = Mock(spec=BaseConnector)  # Most connectors define no mark_sync_complete.
    combined = kb_sync._CombinedConnector([], {}, [first, second, plain])
    combined.mark_sync_complete()
    first.mark_sync_complete.assert_called_once_with()
    second.mark_sync_complete.assert_called_once_with()


def _mock_child(manifest):
    child = MagicMock()
    child.__enter__.return_value = child
    child.__exit__.return_value = False
    child.build_manifest.return_value = manifest
    child.content_addressed_checksums = False
    child.mark_sync_complete = Mock()
    return child


def test_run_entries_sync_advances_checkpoints_on_every_child_including_empty_ones():
    full = _mock_child([ManifestEntry("a.txt", "", "1", 1)])
    full.read_file.return_value = b"a"
    empty = _mock_child([])
    client = Mock()
    client.sync_diff.side_effect = lambda kb, manifest: {"added": manifest}
    kb_sync.run_entries_sync(
        client,
        [{"source": "one", "kb-id": "kb"}, {"source": "two", "kb-id": "kb"}],
        resolve_connector=Mock(side_effect=[full, empty]),
        quiet=True,
    )
    full.mark_sync_complete.assert_called_once_with()
    empty.mark_sync_complete.assert_called_once_with()


def test_combined_connector_forwards_requires_empty_sync():
    requesting = Mock()
    requesting.requires_empty_sync = Mock(return_value=True)
    nonrequesting = Mock()
    nonrequesting.requires_empty_sync = Mock(return_value=False)
    plain = Mock(spec=BaseConnector)  # Most connectors define no requires_empty_sync.
    assert kb_sync._CombinedConnector([], {}, [requesting]).requires_empty_sync() is True
    assert kb_sync._CombinedConnector([], {}, [nonrequesting]).requires_empty_sync() is False
    assert (
        kb_sync._CombinedConnector([], {}, [plain, nonrequesting, requesting]).requires_empty_sync()
        is True
    )


def test_all_denylisted_grouped_source_still_runs_cleanup():
    """A grouped source whose manifest emptied (every carried-forward
    ticket denylisted away) requests an empty sync, so its stale KB files
    are still diffed and cleaned up instead of stranded."""
    child = _mock_child([])
    child.requires_empty_sync = Mock(return_value=True)
    client = Mock()
    client.sync_diff.return_value = {"deleted": [{"file_id": "stale-1"}]}
    result = kb_sync.run_entries_sync(
        client,
        [{"source": "one", "kb-id": "kb"}],
        resolve_connector=Mock(return_value=child),
        quiet=True,
    )
    client.sync_cleanup.assert_called_once_with("kb", ["stale-1"])
    assert result.deleted == 1


def test_unreadable_modified_file_retains_stale_kb_copy():
    """An empty read on a modified file skips the upload but must NOT
    delete the stale KB copy: the KB keeps at least one version."""
    source = Source({"a.txt": b""})
    client = Mock()
    client.sync_diff.return_value = {
        "modified": [
            {"filename": "a.txt", "path": "", "checksum": "old", "size": 5, "stale_file_id": "old-1"}
        ]
    }
    result = kb_sync.run_entries_sync(
        client,
        [{"source": "one", "kb-id": "kb"}],
        resolve_connector=Mock(return_value=source),
        quiet=True,
    )
    client.upload_file.assert_not_called()
    client.sync_cleanup.assert_not_called()
    assert result.warnings and "retained" in result.warnings[0]


class UnreadableSource(BaseConnector):
    def build_manifest(self):
        return [ManifestEntry("a.txt", "", "5", 5)]

    def read_file(self, path, filename):
        raise SourceFileUnavailable("permission denied")

    def close(self):
        pass


def test_source_unavailable_modified_file_retains_stale_kb_copy():
    """SourceFileUnavailable on a modified file skips the upload but must
    NOT delete the stale KB copy."""
    client = Mock()
    client.sync_diff.return_value = {
        "modified": [
            {"filename": "a.txt", "path": "", "checksum": "old", "size": 5, "stale_file_id": "old-1"}
        ]
    }
    result = kb_sync.run_entries_sync(
        client,
        [{"source": "one", "kb-id": "kb"}],
        resolve_connector=Mock(return_value=UnreadableSource()),
        quiet=True,
    )
    client.upload_file.assert_not_called()
    client.sync_cleanup.assert_not_called()
    assert result.warnings and "retained" in result.warnings[0]


def test_modified_file_cleanup_runs_after_successful_upload():
    """A modified file's stale KB copy is removed only after its
    replacement uploads successfully."""
    source = Source({"a.txt": b"new"})
    client = Mock()
    client.sync_diff.return_value = {
        "modified": [
            {"filename": "a.txt", "path": "", "checksum": "new", "size": 3, "stale_file_id": "old-1"}
        ]
    }
    kb_sync.run_entries_sync(
        client,
        [{"source": "one", "kb-id": "kb"}],
        resolve_connector=Mock(return_value=source),
        quiet=True,
    )
    client.upload_file.assert_called_once()
    client.sync_cleanup.assert_called_once_with("kb", ["old-1"], None)
    upload_idx = next(
        i for i, c in enumerate(client.mock_calls) if c[0] == "upload_file"
    )
    cleanup_idx = next(
        i for i, c in enumerate(client.mock_calls) if c[0] == "sync_cleanup"
    )
    assert upload_idx < cleanup_idx


def _duplicate_content_error():
    response = httpx.Response(
        400, json={"detail": "Duplicate content detected."},
        request=httpx.Request("POST", "https://webui.example/files"),
    )
    return httpx.HTTPStatusError("Bad Request", request=response.request, response=response)


def test_duplicate_content_modified_file_deletes_stale_and_retries():
    """A modified file whose new content is byte-identical to its
    still-indexed stale copy is rejected with "Duplicate content
    detected"; the stale copy is deleted and the upload retried so the
    run converges instead of erroring forever."""
    client = Mock()
    client.sync_diff.return_value = {
        "modified": [
            {"filename": "a.txt", "path": "", "checksum": "new", "size": 3, "stale_file_id": "old-1"}
        ]
    }
    client.upload_file.side_effect = [_duplicate_content_error(), Mock()]
    result = kb_sync.run_entries_sync(
        client,
        [{"source": "one", "kb-id": "kb"}],
        resolve_connector=Mock(return_value=Source({"a.txt": b"same"})),
        quiet=True,
    )
    assert client.upload_file.call_count == 2
    client.sync_cleanup.assert_called_once_with("kb", ["old-1"])
    cleanup_idx = next(i for i, c in enumerate(client.mock_calls) if c[0] == "sync_cleanup")
    second_upload_idx = [i for i, c in enumerate(client.mock_calls) if c[0] == "upload_file"][1]
    assert cleanup_idx < second_upload_idx
    assert result.modified == 1
    assert not result.errors


def test_duplicate_content_retry_still_failing_keeps_single_delete():
    """If the retry also hits duplicate content (another file owns the
    hash), the error surfaces and the stale copy is not deleted twice."""
    client = Mock()
    client.sync_diff.return_value = {
        "modified": [
            {"filename": "a.txt", "path": "", "checksum": "new", "size": 3, "stale_file_id": "old-1"}
        ]
    }
    client.upload_file.side_effect = [_duplicate_content_error(), _duplicate_content_error()]
    result = kb_sync.run_entries_sync(
        client,
        [{"source": "one", "kb-id": "kb"}],
        resolve_connector=Mock(return_value=Source({"a.txt": b"same"})),
        quiet=True,
    )
    assert client.upload_file.call_count == 2
    client.sync_cleanup.assert_called_once_with("kb", ["old-1"])
    assert result.errors


def test_duplicate_content_added_file_does_not_delete_anything():
    """Added entries have no stale copy to remove: a duplicate-content
    rejection is a plain error."""
    client = Mock()
    client.sync_diff.return_value = {
        "added": [{"filename": "a.txt", "path": "", "checksum": "new", "size": 3}]
    }
    client.upload_file.side_effect = [_duplicate_content_error()]
    result = kb_sync.run_entries_sync(
        client,
        [{"source": "one", "kb-id": "kb"}],
        resolve_connector=Mock(return_value=Source({"a.txt": b"a"})),
        quiet=True,
    )
    client.upload_file.assert_called_once()
    client.sync_cleanup.assert_not_called()
    assert result.errors


def test_rmdir_only_diff_still_cleans_up():
    """A diff that only removes directories still runs cleanup after the
    (empty) upload step — the rmdir used to run in the pre-upload cleanup
    and must not regress with the reordered cleanup."""
    source = Source({"a.txt": b"x"})
    client = Mock()
    client.sync_diff.return_value = {"rmdir": ["d1"]}
    result = kb_sync.run_entries_sync(
        client,
        [{"source": "one", "kb-id": "kb"}],
        resolve_connector=Mock(return_value=source),
        quiet=True,
    )
    client.upload_file.assert_not_called()
    client.sync_cleanup.assert_called_once_with("kb", [], ["d1"])
    assert result.dirs_removed == 1
