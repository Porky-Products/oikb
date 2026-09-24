"""Sync orchestrator — diff → cleanup → mkdir → upload."""

from __future__ import annotations

import fnmatch
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

import click
import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from oikb.client import OikbClient
from oikb.connectors import BaseConnector, ManifestEntry, SourceFileUnavailable
from oikb.duplicate_failures import DUPLICATE_FAILURE_LIMIT, DuplicateFailureTracker, FailureKey

# Stderr console for progress output (keeps stdout clean for piping).
_console = Console(stderr=True)


@dataclass
class SyncResult:
    """Summary of a completed sync operation."""

    added: int = 0
    modified: int = 0
    deleted: int = 0
    unmodified: int = 0
    duplicate_skipped: int = 0
    duplicate_blocked: int = 0
    dirs_created: int = 0
    dirs_removed: int = 0
    errors: list[str] | None = None
    warnings: list[str] | None = None

    @property
    def total_changes(self) -> int:
        return self.added + self.modified + self.deleted

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"{self.added} added")
        if self.modified:
            parts.append(f"{self.modified} modified")
        if self.deleted:
            parts.append(f"{self.deleted} deleted")
        if self.unmodified:
            parts.append(f"{self.unmodified} unchanged")
        if self.duplicate_skipped:
            parts.append(f"{self.duplicate_skipped} duplicate skipped")
        if self.duplicate_blocked:
            parts.append(f"{self.duplicate_blocked} blocked after repeated duplicate-content failures")
        if self.dirs_created:
            parts.append(f"{self.dirs_created} dirs created")
        if self.dirs_removed:
            parts.append(f"{self.dirs_removed} dirs removed")
        return ", ".join(parts) if parts else "nothing to do"


class SyncCancelled(Exception):
    """Raised when a running sync is asked to stop."""


def parse_size(value: str | int | None) -> int | None:
    """Parse a human-readable size string to bytes.

    Examples: '50mb' → 52428800, '1gb' → 1073741824, '500kb' → 512000.
    Returns None if value is None or empty.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)

    value = value.strip().lower()
    multipliers = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}

    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)].strip()) * mult)

    return int(value)


def build_manifest_filter(
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_size: int | None = None,
) -> Callable[[list[ManifestEntry]], list[ManifestEntry]] | None:
    """Build a filter function from glob include/exclude patterns and size limit.

    Returns None if no filtering is needed.
    """
    if not include and not exclude and max_size is None:
        return None

    def _filter(entries: list[ManifestEntry]) -> list[ManifestEntry]:
        result = []
        for entry in entries:
            path = entry.display_path
            if include and not any(fnmatch.fnmatch(path, p) for p in include):
                continue
            if exclude and any(fnmatch.fnmatch(path, p) for p in exclude):
                continue
            if max_size is not None and entry.size > max_size:
                click.echo(
                    click.style(
                        f"  ⚠ Skipping {path} ({_fmt_size(entry.size)}) "
                        f"— exceeds max-size ({_fmt_size(max_size)})",
                        fg="yellow",
                    ),
                    err=True,
                )
                continue
            result.append(entry)
        return result

    return _filter


def _fmt_size(n: int) -> str:
    """Format bytes as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def run_sync(
    client: OikbClient,
    connector: BaseConnector,
    kb_id: str,
    dry_run: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    manifest_filter: Callable[[list[ManifestEntry]], list[ManifestEntry]] | None = None,
    concurrency: int = 1,
    cancel_requested: Callable[[], bool] | None = None,
    duplicate_failures: DuplicateFailureTracker | None = None,
) -> SyncResult:
    """Execute a full incremental sync.

    Steps:
      1. Build manifest from connector
      2. Apply optional manifest filter
      3. POST manifest to /sync/diff
      4. Cleanup stale files (delete before upload)
      5. Create missing directories
      6. Upload added + modified files
    """
    result = SyncResult()
    result.errors = []
    result.warnings = []
    completed = False

    try:
        sync_result = _run_sync_inner(
            client, connector, kb_id, dry_run, verbose, quiet,
            manifest_filter, concurrency, result, cancel_requested,
            duplicate_failures if not dry_run else None,
        )
        completed = True
        return sync_result
    finally:
        mark_sync_complete = getattr(connector, "mark_sync_complete", None)
        # Advance the checkpoint whenever the sync run completed (even if some
        # uploads failed).  The upload loop already retries 3× for transient
        # errors, so anything still in result.errors after that is likely a
        # persistent problem with a specific file (e.g. an oversized attachment
        # that always times out).  Blocking the checkpoint on those errors means
        # every subsequent run re-processes the entire ticket set from the same
        # start_time, compounding the problem rather than making progress.
        #
        # Trade-off: a ticket whose upload fails permanently will be silently
        # skipped until Zendesk updates it (changing updated_at) and it re-enters
        # the incremental window.  If that's unacceptable, revisit Option C:
        # record failed ticket IDs in connector state and retry them specifically
        # on the next run, independent of the checkpoint.
        if completed and not dry_run and callable(mark_sync_complete):
            mark_sync_complete()
        connector.close()


def _run_sync_inner(
    client: OikbClient,
    connector: BaseConnector,
    kb_id: str,
    dry_run: bool,
    verbose: bool,
    quiet: bool,
    manifest_filter: Callable[[list[ManifestEntry]], list[ManifestEntry]] | None,
    concurrency: int,
    result: SyncResult,
    cancel_requested: Callable[[], bool] | None,
    duplicate_failures: DuplicateFailureTracker | None = None,
) -> SyncResult:
    """Inner sync logic, separated for clean connector cleanup."""
    show_progress = not quiet and not dry_run

    def check_stop() -> None:
        if cancel_requested and cancel_requested():
            raise SyncCancelled("sync cancelled")

    # ── 1. Build manifest ──────────────────────────────────────
    check_stop()
    if show_progress:
        with _console.status("[bold blue]Scanning source..."):
            manifest = connector.build_manifest()
        _console.print(f"  [dim]{len(manifest)} files found[/dim]")
    else:
        if verbose:
            click.echo("Scanning source...", err=True)
        manifest = connector.build_manifest()
        if verbose:
            click.echo(f"  {len(manifest)} files found", err=True)

    # ── 2. Apply filter ────────────────────────────────────────
    check_stop()
    if manifest_filter:
        manifest = manifest_filter(manifest)
        if show_progress:
            _console.print(f"  [dim]{len(manifest)} files after filtering[/dim]")
        elif verbose:
            click.echo(f"  {len(manifest)} files after filtering", err=True)

    if not manifest and duplicate_failures is not None:
        duplicate_failures.retain_pending(set())

    if not manifest:
        requires_empty_sync = getattr(connector, "requires_empty_sync", None)
        if callable(requires_empty_sync) and requires_empty_sync():
            manifest = []
        else:
            if not quiet:
                click.echo("Source is empty — nothing to sync.", err=True)
            return result

    # ── 3. Compute diff ────────────────────────────────────────
    check_stop()
    if show_progress:
        with _console.status("[bold blue]Computing diff..."):
            diff = client.sync_diff(kb_id, [e.to_dict() for e in manifest])
    else:
        if verbose:
            click.echo("Computing diff...", err=True)
        diff = client.sync_diff(kb_id, [e.to_dict() for e in manifest])

    added: list[dict[str, Any]] = diff.get("added") or []
    modified: list[dict[str, Any]] = diff.get("modified") or []
    deleted: list[dict[str, Any]] = diff.get("deleted") or []
    unmodified_count: int = diff.get("unmodified_count", 0)
    mkdir: list[str] = diff.get("mkdir") or []
    rmdir: list[str] = diff.get("rmdir") or []
    directory_map: dict[str, str] = diff.get("directory_map") or {}

    result.unmodified = unmodified_count

    if show_progress:
        parts = []
        if added:
            parts.append(f"[green]+{len(added)}[/green]")
        if modified:
            parts.append(f"[yellow]~{len(modified)}[/yellow]")
        if deleted:
            parts.append(f"[red]-{len(deleted)}[/red]")
        if unmodified_count:
            parts.append(f"[dim]{unmodified_count} unchanged[/dim]")
        _console.print(f"  Diff: {', '.join(parts)}" if parts else "  [dim]Nothing to do[/dim]")

    # ── Dedup guard ────────────────────────────────────────────
    # open-webui rejects uploads whose content hash already exists in
    # the KB (as a *different* file), leaving orphaned File rows that
    # the diff cannot see.  Filter such "added" entries before the
    # dry-run return so dry runs report the same totals the real run
    # would produce.  "modified" entries are left alone: their stale
    # file is only cleaned up after the replacement uploads.  When the
    # new content is byte-identical to the still-indexed stale copy,
    # the upload is rejected with "Duplicate content detected"; the
    # error is surfaced and the stale copy is retained (never deleted
    # before the replacement succeeds), so a failed replacement cannot
    # leave the KB without an indexed copy.
    # Scope: only sound for content-addressed connectors, where
    # checksum equality implies content equality.  (zendesktickets is
    # content-addressed; gdrive is only when md5Checksum is present —
    # its Google-native fallback token hashes id+modifiedTime, so
    # checksum equality there does NOT imply identical content.)
    # Connectors without that guarantee must skip the guard entirely:
    # identical checksums there do not imply identical content, so
    # skipping "duplicate" uploads would drop legitimately distinct
    # files.
    skipped: list[str] = []
    manifest_by_key = {(e.path, e.filename): e for e in manifest}

    def failure_key(entry: dict) -> FailureKey | None:
        item = manifest_by_key.get((entry.get("path", ""), entry["filename"]))
        return (item.path, item.filename, item.checksum) if item else None

    blocked_keys: set[FailureKey] = set()
    if duplicate_failures is not None:
        duplicate_failures.retain_pending({
            key for entry in [*added, *modified] if (key := failure_key(entry)) is not None
        })
        blocked_keys = duplicate_failures.blocked_keys()

    # An existing block must remain an error, even if the dedup guard would
    # otherwise silently skip this added entry on a later run.
    blocked_added = [entry for entry in added if failure_key(entry) in blocked_keys]
    added = [entry for entry in added if failure_key(entry) not in blocked_keys]
    if added and getattr(connector, "content_addressed_checksums", False):
        kb_files = client.list_kb_files(kb_id)
        # Files scheduled for deletion (directly or as the stale half of
        # a modification) do not count as retained content: a rename or
        # move is an added path plus a deleted path carrying the same
        # checksum, and treating that as a duplicate would skip the new
        # path while cleanup removes the only indexed copy.  Exclude
        # them before comparing.  Note the stale half of a modification is
        # only removed after its replacement uploads, so an added entry
        # matching those bytes can still hit a duplicate-content rejection
        # in the same run; the error is surfaced and the entry syncs on the
        # next run (deleting the stale copy first would reintroduce the
        # data-loss bug the deferred cleanup prevents).
        stale_ids = {d["file_id"] for d in deleted if d.get("file_id")}
        stale_ids |= {m["stale_file_id"] for m in modified if m.get("stale_file_id")}
        existing_hashes = {
            h for f in kb_files
            if f.get("id") not in stale_ids
            for h in ((f.get("meta") or {}).get("file_hash"), f.get("hash"))
            if h
        }
        added, skipped = filter_duplicate_uploads(
            added, modified, manifest_by_key, existing_hashes
        )
    added.extend(blocked_added)
    if duplicate_failures is not None:
        duplicate_failures.retain_pending({
            key for entry in [*added, *modified] if (key := failure_key(entry)) is not None
        })
    if skipped:
        result.duplicate_skipped = len(skipped)
        if verbose:
            for display in skipped:
                click.echo(
                    click.style(f"  ⏭ {display}: duplicate content, skipping", fg="yellow"),
                    err=True,
                )
        result.warnings.append(
            f"Skipped {len(skipped)} duplicate upload(s) "
            "(content hash already in KB or duplicated in this run)"
            )

    # ── Dry run: just print what would happen ──────────────────
    if dry_run:
        result.added = len(added)
        result.modified = len(modified)
        result.deleted = len(deleted)
        result.dirs_created = len(mkdir)
        result.dirs_removed = len(rmdir)

        if added:
            click.echo(click.style("+ Added:", fg="green"))
            for f in added:
                _echo_file_entry(f, "+", "green")

        if modified:
            click.echo(click.style("~ Modified:", fg="yellow"))
            for f in modified:
                _echo_file_entry(f, "~", "yellow")

        if deleted:
            click.echo(click.style("- Deleted:", fg="red"))
            for f in deleted:
                _echo_file_entry(f, "-", "red")

        if mkdir:
            click.echo(click.style("📁 Dirs to create:", fg="cyan"))
            for d in mkdir:
                click.echo(f"  + {d}")

        if rmdir:
            click.echo(click.style("📁 Dirs to remove:", fg="cyan"))
            for d in rmdir:
                click.echo(f"  - {d}")

        return result

    # Nothing to do?
    if not added and not modified and not deleted and not mkdir and not rmdir:
        return result

    # ── 4. Cleanup deleted files ───────────────────────────────
    # Only diff-deleted files are removed before upload. A modified
    # entry's stale file is removed after its replacement uploads
    # successfully (see _cleanup_replaced): deleting it first meant an
    # unreadable source (empty read or SourceFileUnavailable) or a failed
    # upload left the KB with neither the old nor the new copy — data
    # loss instead of a harmless skip. Duplicate-content rejections also
    # retain the stale copy: deleting it to retry cannot guarantee that
    # the replacement will succeed.
    deleted_ids = [d["file_id"] for d in deleted]

    if deleted_ids:
        check_stop()
        if show_progress:
            with _console.status(f"[bold blue]Cleaning up {len(deleted_ids)} deleted files..."):
                client.sync_cleanup(kb_id, deleted_ids)
        else:
            if verbose:
                click.echo(f"Cleaning up {len(deleted_ids)} deleted files...", err=True)
            client.sync_cleanup(kb_id, deleted_ids)
        result.deleted = len(deleted)

    # ── 5. Create missing directories ──────────────────────────
    for dir_path in mkdir:
        check_stop()
        segments = dir_path.split("/")
        name = segments[-1]
        parent_path = "/".join(segments[:-1])
        parent_id = directory_map.get(parent_path)

        if verbose:
            click.echo(f"  mkdir {dir_path}", err=True)

        resp = client.create_directory(kb_id, name, parent_id)
        directory_map[dir_path] = resp.get("id", "")
        result.dirs_created += 1

    # ── 6. Upload files ────────────────────────────────────────

    files_to_upload = [
        *[(a, "added") for a in added],
        *[(m, "modified") for m in modified],
    ]

    # Stale files of modified entries whose replacement did not upload
    # (unreadable source, SourceFileUnavailable, or a failed upload) are
    # retained instead of deleted, so the KB keeps at least one version.
    retain_stale: set[str] = set()

    def _cleanup_replaced() -> None:
        """Delete modified entries' stale files after their replacements
        uploaded, and remove emptied directories."""
        stale_modified_ids = [
            m["stale_file_id"] for m in modified
            if m.get("stale_file_id")
            and m["stale_file_id"] not in retain_stale
        ]
        if not stale_modified_ids and not rmdir:
            return
        check_stop()
        if show_progress:
            with _console.status(f"[bold blue]Cleaning up {len(stale_modified_ids)} replaced files..."):
                client.sync_cleanup(kb_id, stale_modified_ids, rmdir if rmdir else None)
        else:
            if verbose:
                click.echo(
                    f"Cleaning up {len(stale_modified_ids)} replaced files, {len(rmdir)} dirs...",
                    err=True,
                )
            client.sync_cleanup(kb_id, stale_modified_ids, rmdir if rmdir else None)
        result.dirs_removed = len(rmdir)

    if not files_to_upload:
        _cleanup_replaced()
        return result

    def blocked_message(display: str) -> str:
        return (
            f"{display}: blocked after {DUPLICATE_FAILURE_LIMIT} consecutive duplicate-content failures; "
            "change the source checksum or explicitly retry blocked files"
        )

    def _upload_one(
        i: int, entry: dict, change_type: str, progress: Progress | None, task_id: Any,
    ) -> tuple[str, str | None]:
        """Upload a single file with retry."""
        filename = entry["filename"]
        path = entry.get("path", "")
        display = f"{path}/{filename}" if path else filename

        if verbose and not progress:
            click.echo(f"  [{i}/{len(files_to_upload)}] {display}", err=True)

        check_stop()
        manifest_entry = manifest_by_key.get((path, filename))
        if not manifest_entry:
            return ("error", f"File not in manifest: {display}")

        if failure_key(entry) in blocked_keys:
            if progress is not None:
                progress.update(task_id, advance=1, description=f"[red]✗ {display}[/red]")
            return ("blocked", blocked_message(display))

        last_err: Exception | None = None
        error_kind = "error"
        for attempt in range(3):
            check_stop()
            uploading = False
            try:
                content = connector.read_file(path, filename)
                if not content:
                    note = f"{display}: empty content, skipping"
                    if change_type == "modified":
                        note += " — existing KB copy retained"
                    if progress is not None:
                        progress.update(task_id, advance=1, description=f"[yellow]⚠ {display}[/yellow]")
                    else:
                        click.echo(click.style(f"  ⚠ {note}", fg="yellow"), err=True)
                    return ("warning", note)
                check_stop()
                directory_id = directory_map.get(path) if path else None
                uploading = True
                client.upload_file(
                    file_content=content,
                    filename=filename,
                    kb_id=kb_id,
                    file_hash=manifest_entry.checksum,
                    directory_id=directory_id,
                )
                if progress is not None:
                    progress.update(task_id, advance=1, description=f"[cyan]{display}[/cyan]")
                return (change_type, None)
            except SourceFileUnavailable as e:
                message = f"{display}: {e}"
                if change_type == "modified":
                    message += " — existing KB copy retained"
                if progress is not None:
                    progress.update(task_id, advance=1, description=f"[yellow]⚠ {display}[/yellow]")
                else:
                    click.echo(click.style(f"  ⚠ {message}", fg="yellow"), err=True)
                return ("warning", message)
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < 2:
                    time.sleep(2 ** attempt)
                    check_stop()
                    last_err = e
                    continue
                detail = e.response.text.strip()
                try:
                    payload = e.response.json()
                    if isinstance(payload, dict) and payload.get("detail"):
                        detail = str(payload["detail"])
                except ValueError:
                    # Non-JSON error bodies are expected (plain-text or HTML
                    # gateway errors); deliberately fall back to response.text.
                    pass
                if uploading and e.response.status_code == 400 and "duplicate content" in detail.lower():
                    error_kind = "duplicate"
                # A duplicate rejection is terminal too. Even a matching
                # stale hash cannot make delete-then-retry safe: the next
                # upload could fail and leave no indexed copy.
                if change_type == "modified" and entry.get("stale_file_id"):
                    detail = f"{detail} — existing KB copy retained"
                last_err = RuntimeError(f"{e} — {detail}") if detail else e
                break
            except httpx.TimeoutException as e:
                # Timeouts otherwise surface as generic errors and can leave an
                # unlinked "pending" row on the server; retry like a 5xx.
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    check_stop()
                    last_err = e
                    continue
                last_err = e
                break
            except SyncCancelled:
                raise
            except Exception as e:
                last_err = e
                break

        if progress is not None:
            progress.update(task_id, advance=1, description=f"[red]✗ {display}[/red]")
        else:
            click.echo(click.style(f"  ✗ {display}: {last_err}", fg="red"), err=True)
        return (error_kind, f"{display}: {last_err}")

    def _tally(outcome: tuple[str, str | None], entry: dict, change_type: str) -> None:
        """Update result counters from an upload outcome."""
        kind, message = outcome
        key = failure_key(entry)
        if duplicate_failures is not None and key is not None:
            if kind == "duplicate":
                if duplicate_failures.record_duplicate(key):
                    kind = "blocked"
                    display = f"{key[0]}/{key[1]}" if key[0] else key[1]
                    message = f"{message}; {blocked_message(display)}"
            elif kind != "blocked":
                duplicate_failures.reset(key)
        if kind == "blocked":
            result.duplicate_blocked += 1
        if kind == "added":
            result.added += 1
        elif kind == "modified":
            result.modified += 1
        else:
            # The replacement did not upload: retain the stale KB copy
            # (removed after success by _cleanup_replaced).
            stale_id = entry.get("stale_file_id")
            if change_type == "modified" and stale_id:
                retain_stale.add(stale_id)
            if kind == "warning" and message is not None:
                result.warnings.append(message)
            elif message is not None:
                result.errors.append(message)

    if show_progress:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]Uploading"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TextColumn("•"),
            TextColumn("{task.description}"),
            TextColumn("•"),
            TimeElapsedColumn(),
            console=_console,
            transient=True,
        )
        with progress:
            task_id = progress.add_task("", total=len(files_to_upload))

            if concurrency > 1 and len(files_to_upload) > 1:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = {
                        pool.submit(_upload_one, i, entry, ct, progress, task_id): (entry, ct)
                        for i, (entry, ct) in enumerate(files_to_upload, 1)
                    }
                    for future in as_completed(futures):
                        entry, ct = futures[future]
                        _tally(future.result(), entry, ct)
            else:
                for i, (entry, change_type) in enumerate(files_to_upload, 1):
                    _tally(_upload_one(i, entry, change_type, progress, task_id), entry, change_type)
    else:
        # Quiet or daemon mode — no progress bar.
        if concurrency > 1 and len(files_to_upload) > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(_upload_one, i, entry, ct, None, None): (entry, ct)
                    for i, (entry, ct) in enumerate(files_to_upload, 1)
                }
                for future in as_completed(futures):
                    entry, ct = futures[future]
                    _tally(future.result(), entry, ct)
        else:
            for i, (entry, change_type) in enumerate(files_to_upload, 1):
                _tally(_upload_one(i, entry, change_type, None, None), entry, change_type)

    # ── 7. Cleanup replaced files and removed dirs ────────────
    _cleanup_replaced()

    return result


def filter_duplicate_uploads(
    added: list[dict[str, Any]],
    modified: list[dict[str, Any]],
    manifest_by_key: dict[tuple[str, str], ManifestEntry],
    existing_hashes: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop "added" entries whose content hash is already present in the KB
    (or will be after this run's "modified" uploads, or is duplicated within
    this run).

    open-webui rejects an upload whose content hash exists as a *different*
    file (``Duplicate content detected``), so such uploads can never succeed
    and each attempt leaves an orphaned File row.  Content already lives in
    the KB via the other copy, so skipping is retrievably equivalent.

    Returns ``(filtered_added, skipped_display_names)``.

    ``existing_hashes`` come from open-webui (full 64-char SHA-256 digests);
    manifest checksums are 16-char digests, so KB-side hashes are also
    prefix-truncated when building the comparison set.

    Caller must exclude hashes of files being removed this run (deleted
    entries and stale halves of modifications) from ``existing_hashes``;
    otherwise a rename/move — an added path plus a deleted path sharing
    a checksum — would be skipped while cleanup removes the only
    indexed copy.  Callers must also only invoke this guard when the
    connector's checksums are content hashes (not change-detection
    tokens like gdrive's id+modifiedTime digest).
    """
    # Prefix-truncate KB-side hashes: manifest checksums are 16-char
    # digests (sha256/[:16] by convention) and would never equal the
    # full-length digests open-webui stores.
    prefix_hashes = {
        h[:16] for h in existing_hashes if len(h) > 16
    } | set(existing_hashes)
    modified_hashes = {
        me.checksum
        for m in modified
        if (me := manifest_by_key.get((m.get("path", ""), m["filename"])))
    }
    seen_in_run: set[str] = set()
    filtered: list[dict[str, Any]] = []
    skipped: list[str] = []
    for a in added:
        me = manifest_by_key.get((a.get("path", ""), a["filename"]))
        checksum = me.checksum if me else None
        display = f"{a.get('path', '')}/{a['filename']}"
        if checksum and (
            checksum in prefix_hashes
            or checksum in modified_hashes
            or checksum in seen_in_run
        ):
            skipped.append(display)
            continue
        if checksum:
            seen_in_run.add(checksum)
        filtered.append(a)
    return filtered, skipped


def _echo_file_entry(entry: dict, prefix: str, color: str) -> None:
    """Print a file entry with color."""
    path = entry.get("path", "")
    filename = entry["filename"]
    display = f"{path}/{filename}" if path else filename
    click.echo(click.style(f"  {prefix} {display}", fg=color))
