"""Sync all configured sources for a KB with one complete manifest."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace

from oikb.client import OikbClient
from oikb.connectors import BaseConnector, ManifestEntry
from oikb.duplicate_failures import DuplicateFailureTracker
from oikb.sync import (
    SyncCancelled,
    SyncResult,
    build_manifest_filter,
    parse_size,
    run_sync,
)


def group_entries_by_kb(entries: list[dict]) -> list[list[dict]]:
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        if not entry.get("source") or not entry.get("kb-id"):
            raise ValueError("Each source requires source and kb-id")
        group = groups.setdefault(entry["kb-id"], [])
        if group and any(entry.get(key) != group[0].get(key) for key in ("url", "token")):
            raise ValueError(f"Sources for KB {entry['kb-id']} must use the same url and token")
        group.append(entry)
    return list(groups.values())


class _CombinedConnector(BaseConnector):
    def __init__(
        self,
        manifest: list[ManifestEntry],
        routes: dict[tuple[str, str], tuple[BaseConnector, str]],
        children: list[BaseConnector],
    ):
        self._manifest = manifest
        self._routes = routes
        self._children = children

    def build_manifest(self) -> list[ManifestEntry]:
        return self._manifest

    def read_file(self, path: str, filename: str) -> bytes:
        connector, original_path = self._routes[(path, filename)]
        return connector.read_file(original_path, filename)

    @property
    def content_addressed_checksums(self) -> bool:
        # sync gates its duplicate-upload guard on this flag; expose it only
        # when every child guarantees checksum equality implies content equality.
        return all(
            getattr(child, "content_addressed_checksums", False) for child in self._children
        )

    def mark_sync_complete(self) -> None:
        # sync invokes this once per completed run; forward to each child
        # exactly once so checkpointing connectors persist their progress.
        for child in self._children:
            mark = getattr(child, "mark_sync_complete", None)
            if callable(mark):
                mark()

    def requires_empty_sync(self) -> bool:
        # sync treats an empty manifest as "nothing to sync" unless the
        # connector requests an empty sync (e.g. every carried-forward
        # ticket was denylisted away). Forward so a grouped run whose only
        # source became empty still runs sync_diff/sync_cleanup instead of
        # stranding the KB's stale files.
        return any(
            callable(req) and req()
            for req in (
                getattr(child, "requires_empty_sync", None) for child in self._children
            )
        )


def run_entries_sync(
    client: OikbClient,
    entries: list[dict],
    *,
    resolve_connector: Callable[..., BaseConnector],
    dry_run: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    max_file_size: str | None = None,
    concurrency: int = 1,
    cancel_requested: Callable[[], bool] | None = None,
    duplicate_failures: DuplicateFailureTracker | None = None,
) -> SyncResult:
    """Scan and filter every source before allowing a KB-wide diff or deletion.

    Filters apply to source paths. target-path adds a destination directory;
    multi-source Confluence entries default to their space key as a prefix.
    ExitStack owns connectors, including when resolution or scanning fails.
    """
    groups = group_entries_by_kb(entries)
    if len(groups) != 1:
        raise ValueError("Expected sources for exactly one KB")
    manifest: list[ManifestEntry] = []
    routes: dict[tuple[str, str], tuple[BaseConnector, str]] = {}
    children: list[BaseConnector] = []
    with ExitStack() as stack:
        for entry in entries:
            if cancel_requested and cancel_requested():
                raise SyncCancelled("sync cancelled")
            prefix = entry.get("target-path", "")
            if "target-path" not in entry and len(entries) > 1 and entry["source"].startswith("confluence:"):
                from oikb.connectors.confluence import parse_confluence_source
                prefix = parse_confluence_source(entry["source"])["space_key"]
            if not isinstance(prefix, str) or (prefix and any(
                part in {"", ".", ".."} for part in prefix.split("/")
            )) or "\\" in prefix:
                raise ValueError("target-path must be a relative directory path")
            filters = entry.get("filter", {})
            manifest_filter = build_manifest_filter(
                include=filters.get("include"),
                exclude=filters.get("exclude"),
                max_size=parse_size(filters.get("max-size") or max_file_size),
            )
            connector = stack.enter_context(resolve_connector(
                entry["source"], branch=entry.get("branch"), path=entry.get("path"),
                auth=entry.get("auth", {}),
            ))
            children.append(connector)
            source_manifest = connector.build_manifest()
            if manifest_filter:
                source_manifest = manifest_filter(source_manifest)
            for item in source_manifest:
                path = "/".join(p for p in (prefix, item.path) if p)
                key = (path, item.filename)
                if key in routes:
                    raise ValueError(f"Duplicate manifest path: {path}/{item.filename}. Set distinct target-path values.")
                routes[key] = (connector, item.path)
                manifest.append(replace(item, path=path))

        return run_sync(
            client=client,
            connector=_CombinedConnector(manifest, routes, children),
            kb_id=entries[0]["kb-id"],
            dry_run=dry_run,
            verbose=verbose,
            quiet=quiet,
            concurrency=max(entry.get("concurrency", concurrency) for entry in entries),
            cancel_requested=cancel_requested,
            duplicate_failures=duplicate_failures,
        )
