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


class TestSyncResultSummary:
    def test_duplicate_skipped_in_summary(self):
        result = SyncResult(added=2, duplicate_skipped=5)
        assert "5 duplicate skipped" in result.summary()
        assert "2 added" in result.summary()

    def test_duplicate_skipped_zero_omitted(self):
        result = SyncResult(added=1)
        assert "duplicate" not in result.summary()
