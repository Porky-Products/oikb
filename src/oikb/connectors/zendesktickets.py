"""Zendesk Tickets connector — sync tickets and attachments to a Knowledge Base."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


class ZendeskTicketsConnector(BaseConnector):
    def __init__(
        self,
        subdomain: str | None = None,
        user: str | None = None,
        token: str | None = None,
        state_dir: str | None = None,
    ):
        self._subdomain = subdomain or os.environ.get("ZENDESKTICKET_SUBDOMAIN", "")
        self._user = user or os.environ.get("ZENDESKTICKET_USER", "")
        self._token = token or os.environ.get("ZENDESKTICKET_TOKEN", "")
        self._page_size = int(os.environ.get("ZENDESKTICKET_PAGE_SIZE", "10"))
        self._download_attachments = _parse_bool(os.environ.get("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "false"))
        self._include_tags = _parse_tags(os.environ.get("ZENDESKTICKET_INCLUDETAGS", ""))
        self._exclude_tags = _parse_tags(os.environ.get("ZENDESKTICKET_EXCLUDETAGS", ""))
        if not self._subdomain or not self._user or not self._token:
            raise ValueError(
                "Zendesk tickets credentials required. Set ZENDESKTICKET_SUBDOMAIN, "
                "ZENDESKTICKET_USER, and ZENDESKTICKET_TOKEN env vars."
            )

        default_state_dir = Path.cwd() / ".oikb_state" / "zendesktickets" / self._subdomain
        self._state_dir = Path(state_dir) if state_dir else default_state_dir
        self._http = httpx.Client(
            base_url=f"https://{self._subdomain}.zendesk.com/api/v2",
            auth=(f"{self._user}/token", self._token),
            timeout=30.0,
        )
        self._file_cache: dict[tuple[str, str], bytes] = {}
        self._manifest_snapshot: dict[str, Any] = {}
        self._pending_checkpoint: datetime | None = None

    def build_manifest(self) -> list[ManifestEntry]:
        state = self._load_state()
        checkpoint = self._load_checkpoint(state)
        prior_entries = self._state_entries_by_ticket(state)
        attachments_enabled_previously = bool(state.get("attachments_enabled", False))

        self._file_cache.clear()
        current_entries_by_ticket: dict[str, list[ManifestEntry]] = {}
        excluded_ticket_ids: set[str] = set()
        pending_checkpoint = checkpoint
        seen_ticket_ids: set[str] = set()

        for page in self._iter_ticket_pages(checkpoint):
            page_max_updated_at: datetime | None = None
            for ticket in page:
                ticket_id = str(ticket["id"])
                updated_at = self._parse_dt(ticket["updated_at"])
                page_max_updated_at = updated_at if page_max_updated_at is None else max(page_max_updated_at, updated_at)
                seen_ticket_ids.add(ticket_id)

                if not self._should_include_ticket(ticket):
                    excluded_ticket_ids.add(ticket_id)
                    continue

                comments = self._fetch_ticket_comments(ticket["id"])
                entries = self._build_ticket_entries(ticket, comments)
                current_entries_by_ticket[ticket_id] = entries

            if page_max_updated_at is not None:
                pending_checkpoint = page_max_updated_at

        carried_forward = {
            ticket_id: entries
            for ticket_id, entries in prior_entries.items()
            if ticket_id not in seen_ticket_ids and ticket_id not in excluded_ticket_ids
        }

        if attachments_enabled_previously and not self._download_attachments:
            for ticket_id, entries in list(carried_forward.items()):
                carried_forward[ticket_id] = [entry for entry in entries if not entry.path]

        combined_entries_by_ticket = carried_forward | current_entries_by_ticket
        manifest = [entry for entries in combined_entries_by_ticket.values() for entry in entries]
        manifest.sort(key=lambda entry: entry.display_path)

        self._manifest_snapshot = {
            "attachments_enabled": self._download_attachments,
            "ticket_files": {
                ticket_id: {
                    "entries": [entry.to_dict() for entry in entries],
                    "updated_at": self._ticket_updated_at(ticket_id, state, current_entries_by_ticket),
                }
                for ticket_id, entries in combined_entries_by_ticket.items()
            },
        }
        self._pending_checkpoint = pending_checkpoint
        return manifest

    def read_file(self, path: str, filename: str) -> bytes:
        content = self._file_cache.get((path, filename))
        if content is None:
            raise FileNotFoundError(f"Ticket file not found: {path}/{filename}" if path else f"Ticket file not found: {filename}")
        return content

    def mark_sync_complete(self) -> None:
        if self._pending_checkpoint is not None:
            self._save_checkpoint(self._pending_checkpoint)
        self._save_state(self._manifest_snapshot)

    def has_content(self) -> bool:
        return bool(self._manifest_snapshot.get("ticket_files"))

    def requires_empty_sync(self) -> bool:
        return bool(self._manifest_snapshot) and not self.has_content()

    def close(self) -> None:
        self._http.close()

    def _build_ticket_entries(self, ticket: dict[str, Any], comments: list[dict[str, Any]]) -> list[ManifestEntry]:
        ticket_id = str(ticket["id"])
        markdown = self._render_ticket_markdown(ticket, comments).encode("utf-8")
        entries = [self._cache_entry(path="", filename=f"{ticket_id}.md", content=markdown)]

        if self._download_attachments:
            attachments = self._collect_attachments(ticket, comments)
            for attachment in attachments:
                content = self._download_attachment(attachment["content_url"])
                filename = f"{ticket_id}-{self._sanitize_filename(attachment['file_name'])}"
                entries.append(self._cache_entry(path=ticket_id, filename=filename, content=content))

        return entries

    def _cache_entry(self, path: str, filename: str, content: bytes) -> ManifestEntry:
        self._file_cache[(path, filename)] = content
        return ManifestEntry(
            filename=filename,
            path=path,
            checksum=hashlib.sha256(content).hexdigest()[:16],
            size=len(content),
        )

    def _iter_ticket_pages(self, checkpoint: datetime) -> Iterator[list[dict[str, Any]]]:
        page = 1
        while True:
            response = self._http.get(
                "/tickets.json",
                params={
                    "page": page,
                    "per_page": self._page_size,
                    "sort_by": "updated_at",
                    "sort_order": "asc",
                    "start_time": self._format_start_time(checkpoint),
                },
            )
            response.raise_for_status()
            payload = response.json()
            tickets = payload.get("tickets", [])
            yield tickets
            if not payload.get("next_page"):
                break
            page += 1

    def _fetch_ticket_comments(self, ticket_id: int) -> list[dict[str, Any]]:
        response = self._http.get(f"/tickets/{ticket_id}/comments.json")
        response.raise_for_status()
        return response.json().get("comments", [])

    def _render_ticket_markdown(self, ticket: dict[str, Any], comments: list[dict[str, Any]]) -> str:
        lines = [
            f"# Ticket {ticket['id']}: {ticket.get('subject') or 'Untitled'}",
            "",
            f"Status: {ticket.get('status') or 'unknown'}",
            f"Priority: {ticket.get('priority') or 'unassigned'}",
            f"Updated at: {ticket.get('updated_at') or ''}",
        ]
        tags = ticket.get("tags") or []
        if tags:
            lines.append(f"Tags: {', '.join(tags)}")

        description = ticket.get("description") or ""
        lines.extend(["", "## Description", "", description or "_No description._", "", "## Comments", ""])

        if not comments:
            lines.append("_No comments._")
        else:
            for comment in comments:
                lines.extend(
                    [
                        f"### Comment {comment.get('id')}",
                        f"Author ID: {comment.get('author_id')}",
                        f"Created at: {comment.get('created_at') or ''}",
                        "",
                        comment.get("body") or "",
                        "",
                    ]
                )
        return "\n".join(lines).strip() + "\n"

    def _collect_attachments(self, ticket: dict[str, Any], comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        attachments = list(ticket.get("attachments") or [])
        for comment in comments:
            attachments.extend(comment.get("attachments") or [])
        return attachments

    def _download_attachment(self, url: str) -> bytes:
        response = self._http.get(url)
        response.raise_for_status()
        return response.content

    def _should_include_ticket(self, ticket: dict[str, Any]) -> bool:
        tags = {str(tag).strip().lower() for tag in ticket.get("tags") or [] if str(tag).strip()}
        if self._include_tags and not (tags & self._include_tags):
            return False
        if self._exclude_tags and (tags & self._exclude_tags):
            return False
        return True

    def _checkpoint_path(self) -> Path:
        return self._state_dir / "resume_checkpoint.txt"

    def _state_path(self) -> Path:
        return self._state_dir / "manifest_state.json"

    def _load_checkpoint(self, state: dict[str, Any]) -> datetime:
        path = self._checkpoint_path()
        if path.exists():
            return self._parse_dt(path.read_text().strip())
        if checkpoint := state.get("checkpoint"):
            return self._parse_dt(checkpoint)
        return datetime.min.replace(tzinfo=UTC)

    def _save_checkpoint(self, value: datetime) -> None:
        path = self._checkpoint_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._format_dt(value))

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = dict(state)
        if self._pending_checkpoint is not None:
            snapshot["checkpoint"] = self._format_dt(self._pending_checkpoint)
        path.write_text(json.dumps(snapshot, indent=2, sort_keys=True))

    def _state_entries_by_ticket(self, state: dict[str, Any]) -> dict[str, list[ManifestEntry]]:
        result: dict[str, list[ManifestEntry]] = {}
        for ticket_id, ticket_state in (state.get("ticket_files") or {}).items():
            result[ticket_id] = [ManifestEntry(**entry) for entry in ticket_state.get("entries", [])]
        return result

    def _ticket_updated_at(self, ticket_id: str, state: dict[str, Any], current_entries_by_ticket: dict[str, list[ManifestEntry]]) -> str:
        if ticket_id in current_entries_by_ticket:
            for path, filename in self._file_cache:
                if filename == f"{ticket_id}.md" and path == "":
                    content = self._file_cache[(path, filename)].decode("utf-8")
                    for line in content.splitlines():
                        if line.startswith("Updated at: "):
                            return line.removeprefix("Updated at: ").strip()
        return ((state.get("ticket_files") or {}).get(ticket_id, {})).get("updated_at", "")

    def _parse_dt(self, value: str) -> datetime:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(UTC)

    def _format_dt(self, value: datetime) -> str:
        return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _format_start_time(self, value: datetime) -> int:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        return 0 if value <= epoch else int(value.timestamp())

    def _sanitize_filename(self, filename: str) -> str:
        return re.sub(r'[<>:"/\\|?*]+', "_", filename)


def parse_zendesktickets_source(source: str) -> dict[str, str | None]:
    subdomain = source.removeprefix("zendesktickets:")
    if not subdomain:
        raise ValueError("Invalid Zendesk tickets source. Expected: zendesktickets:<subdomain>")
    return {"subdomain": subdomain}


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_tags(value: str) -> set[str]:
    return {part.strip().lower() for part in value.split(",") if part.strip()}
