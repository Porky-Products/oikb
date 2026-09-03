"""Tests for the duplicate-upload guard in sync.py."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from oikb.connectors import ManifestEntry
from oikb.sync import SyncResult, filter_duplicate_uploads


def _me(filename: str, path: str, checksum: str) -> ManifestEntry:
    return ManifestEntry(
        filename=filename, path=path, checksum=checksum, size=len(checksum)
    )


def _entry(filename: str, path: str = "") -> dict:
    return {"filename": filename, "path": path}


class TestFilterDuplicateUploads:
    def test_no_duplicates_all_kept(self):
        manifest_by_key = {
            ("tickets/1", "a.txt"): _me("a.txt", "tickets/1", "hash-a"),
            ("tickets/1", "b.txt"): _me("b.txt", "tickets/1", "hash-b"),
        }
        added = [_entry("a.txt", "tickets/1"), _entry("b.txt", "tickets/1")]
        filtered, skipped = filter_duplicate_uploads(
            added, [], manifest_by_key, existing_hashes=set()
        )
        assert filtered == added
        assert skipped == []

    def test_hash_already_in_kb_skipped(self):
        manifest_by_key = {("tickets/1", "a.txt"): _me("a.txt", "tickets/1", "hash-a")}
        added = [_entry("a.txt", "tickets/1")]
        filtered, skipped = filter_duplicate_uploads(
            added, [], manifest_by_key, existing_hashes={"hash-a"}
        )
        assert filtered == []
        assert skipped == ["tickets/1/a.txt"]

    def test_duplicate_within_run_first_kept(self):
        manifest_by_key = {
            ("tickets/1", "a.txt"): _me("a.txt", "tickets/1", "same"),
            ("tickets/2", "a.txt"): _me("a.txt", "tickets/2", "same"),
        }
        added = [_entry("a.txt", "tickets/1"), _entry("a.txt", "tickets/2")]
        filtered, skipped = filter_duplicate_uploads(
            added, [], manifest_by_key, existing_hashes=set()
        )
        assert filtered == [_entry("a.txt", "tickets/1")]
        assert skipped == ["tickets/2/a.txt"]

    def test_collides_with_modified_upload_skipped(self):
        manifest_by_key = {
            ("tickets/1", "a.txt"): _me("a.txt", "tickets/1", "same"),
            ("tickets/2", "a.txt"): _me("a.txt", "tickets/2", "same"),
        }
        added = [_entry("a.txt", "tickets/2")]
        modified = [{**_entry("a.txt", "tickets/1"), "stale_file_id": "f1"}]
        filtered, skipped = filter_duplicate_uploads(
            added, modified, manifest_by_key, existing_hashes=set()
        )
        assert filtered == []
        assert skipped == ["tickets/2/a.txt"]

    def test_full_length_kb_hash_prefix_matched(self):
        # open-webui stores full 64-char digests; manifest checksums are
        # 16-char prefixes. The guard must treat them as equal.
        full = "a" * 64
        manifest_by_key = {("tickets/1", "a.txt"): _me("a.txt", "tickets/1", full[:16])}
        added = [_entry("a.txt", "tickets/1")]
        filtered, skipped = filter_duplicate_uploads(
            added, [], manifest_by_key, existing_hashes={full}
        )
        assert filtered == []
        assert skipped == ["tickets/1/a.txt"]

    def test_entry_missing_from_manifest_kept(self):
        # A diff entry with no manifest record has no checksum; safest to
        # keep it and let the upload path report its own error.
        added = [{"filename": "ghost.txt", "path": "tickets/9"}]
        filtered, skipped = filter_duplicate_uploads(
            added, [], manifest_by_key={}, existing_hashes=set()
        )
        assert filtered == added
        assert skipped == []

    def test_empty_added_noop(self):
        filtered, skipped = filter_duplicate_uploads(
            [], [], manifest_by_key={}, existing_hashes={"x"}
        )
        assert filtered == []
        assert skipped == []


def test_rename_reuploads_file_sharing_hash_with_deleted_file():
    """A moved file (same content, new path) must not be skipped as duplicate.

    sync_diff reports the old path as deleted (removing the only KB copy) and
    the new path as added. The guard must ignore hashes of files this run
    deletes, or the added upload is skipped and the content lost.
    """
    kb_files = [{"id": "f1", "hash": "abc", "meta": {"file_hash": "abc"}}]
    stale = {"f1"}
    existing = {f["hash"] for f in kb_files if f.get("id") not in stale}
    manifest_by_key = {("tickets/new", "1001.md"): _me("1001.md", "tickets/new", "abc")}
    diff_added = [{"path": "tickets/new", "filename": "1001.md", "checksum": "abc", "size": 10}]

    added, _dup = filter_duplicate_uploads(diff_added, [], manifest_by_key, existing)

    assert added == diff_added


def test_rename_regression_guard_off_would_skip_upload():
    """Without stale-id exclusion the rename upload WOULD be skipped.

    Documents the original bug: existing_hashes included the to-be-deleted
    file's hash, so filter_duplicate_uploads dropped the added entry.
    """
    kb_files = [{"id": "f1", "hash": "abc", "meta": {"file_hash": "abc"}}]
    manifest_by_key = {("tickets/new", "1001.md"): _me("1001.md", "tickets/new", "abc")}
    diff_added = [{"path": "tickets/new", "filename": "1001.md", "checksum": "abc", "size": 10}]

    # Buggy call site: no stale exclusion.
    buggy_existing = {f["hash"] for f in kb_files}

    added, _dup = filter_duplicate_uploads(diff_added, [], manifest_by_key, buggy_existing)
    assert added == []

    # Fixed call site: exclude stale ids.
    fixed_existing = {f["hash"] for f in kb_files if f.get("id") not in {"f1"}}
    added, _dup = filter_duplicate_uploads(diff_added, [], manifest_by_key, fixed_existing)
    assert added == diff_added


class TestDedupCapabilityGate:
    def test_base_connector_gate_defaults_off(self):
        from oikb.connectors import BaseConnector

        assert BaseConnector.content_addressed_checksums is False

    def test_zendesk_connector_gate_on(self):
        from oikb.connectors.zendesktickets import ZendeskTicketsConnector

        assert ZendeskTicketsConnector.content_addressed_checksums is True


class TestSyncResultSummary:
    def test_duplicate_skipped_in_summary(self):
        result = SyncResult(added=2, duplicate_skipped=5)
        assert "5 duplicate skipped" in result.summary()
        assert "2 added" in result.summary()

    def test_duplicate_skipped_zero_omitted(self):
        result = SyncResult(added=1)
        assert "duplicate" not in result.summary()
