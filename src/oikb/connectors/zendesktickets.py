"""Zendesk Tickets connector skeleton."""

from __future__ import annotations

import hashlib
import os
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
        default_state_dir = Path.cwd() / ".oikb_state" / "zendesktickets" / self._subdomain
        self._state_dir = Path(state_dir) if state_dir else default_state_dir
        if not self._subdomain or not self._user or not self._token:
            raise ValueError(
                "Zendesk tickets credentials required. Set ZENDESKTICKET_SUBDOMAIN, ZENDESKTICKET_USER, and ZENDESKTICKET_TOKEN env vars."
            )
        self._http = httpx.Client(
            base_url=f"https://{self._subdomain}.zendesk.com/api/v2",
            auth=(f"{self._user}/token", self._token),
            timeout=30.0,
        )
        self._file_cache: dict[tuple[str, str], bytes] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        checkpoint = self._load_checkpoint()
        manifest: list[ManifestEntry] = []
        self._file_cache.clear()

        for page in self._iter_ticket_pages(checkpoint):
            page_max_updated_at: datetime | None = None
            for ticket in page:
                updated_at = self._parse_dt(ticket["updated_at"])
                page_max_updated_at = updated_at if page_max_updated_at is None else max(page_max_updated_at, updated_at)
                if not self._should_include_ticket(ticket):
                    continue
                comments = self._fetch_ticket_comments(ticket["id"])
                markdown = self._render_ticket_markdown(ticket, comments)
                content = markdown.encode("utf-8")
                filename = f"{ticket['id']}.md"
                manifest.append(
                    ManifestEntry(
                        filename=filename,
                        path="",
                        checksum=hashlib.sha256(content).hexdigest()[:16],
                        size=len(content),
                    )
                )
                self._file_cache[("", filename)] = content
            if page_max_updated_at is not None:
                self._save_checkpoint(page_max_updated_at)

        manifest.sort(key=lambda entry: entry.display_path)
        return manifest

    def read_file(self, path: str, filename: str) -> bytes:
        content = self._file_cache.get((path, filename))
        if content is None:
            raise FileNotFoundError(f"Ticket not found: {filename}")
        return content

    def close(self) -> None:
        close = getattr(self._http, "close", None)
        if callable(close):
            close()

    def _checkpoint_path(self) -> Path:
        return self._state_dir / "resume_checkpoint.txt"

    def _load_checkpoint(self) -> datetime:
        path = self._checkpoint_path()
        if not path.exists():
            return datetime.min.replace(tzinfo=UTC)
        return self._parse_dt(path.read_text().strip())

    def _save_checkpoint(self, value: datetime) -> None:
        path = self._checkpoint_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._format_dt(value))

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
                    "updated_since": self._format_dt(checkpoint),
                },
            )
            response.raise_for_status()
            payload = response.json()
            yield payload.get("tickets", [])
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

        if comments:
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
        else:
            lines.append("_No comments._")

        return "\n".join(lines).strip() + "\n"

    def _should_include_ticket(self, ticket: dict[str, Any]) -> bool:
        tags = {str(tag).strip() for tag in ticket.get("tags") or [] if str(tag).strip()}
        if self._include_tags and not (tags & self._include_tags):
            return False
        if self._exclude_tags and (tags & self._exclude_tags):
            return False
        return True

    def _parse_dt(self, value: str) -> datetime:
        normalized = value.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(UTC)

    def _format_dt(self, value: datetime) -> str:
        return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_zendesktickets_source(source: str) -> dict[str, str | None]:
    subdomain = source.removeprefix("zendesktickets:")
    if not subdomain:
        raise ValueError("Invalid Zendesk tickets source. Expected: zendesktickets:<subdomain>")
    return {"subdomain": subdomain}


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_tags(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}
