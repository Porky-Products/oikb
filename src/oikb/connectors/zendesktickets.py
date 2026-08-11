"""Zendesk Tickets connector — sync tickets and attachments to a Knowledge Base."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from oikb.connectors import BaseConnector, ManifestEntry

_ZENDESK_ATTACHMENT_REDIRECT_CODES = {301, 302, 303, 307, 308}
_ZENDESK_RATE_LIMIT_STATUS = 429


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
        self._page_size = _parse_page_size(os.environ.get("ZENDESKTICKET_PAGE_SIZE", "10"))
        self._download_attachments = _parse_bool(os.environ.get("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "false"))
        self._verbose_http = _parse_bool(os.environ.get("ZENDESKTICKET_VERBOSE_HTTP", "false"))
        self._max_retries = _parse_non_negative_int(os.environ.get("ZENDESKTICKET_MAX_RETRIES", "5"), "ZENDESKTICKET_MAX_RETRIES")
        self._backoff_base_seconds = _parse_positive_float(
            os.environ.get("ZENDESKTICKET_BACKOFF_BASE_SECONDS", "1.0"),
            "ZENDESKTICKET_BACKOFF_BASE_SECONDS",
        )
        self._backoff_max_seconds = _parse_positive_float(
            os.environ.get("ZENDESKTICKET_BACKOFF_MAX_SECONDS", "90.0"),
            "ZENDESKTICKET_BACKOFF_MAX_SECONDS",
        )
        include_tags_value = os.environ.get("ZENDESKTICKET_INCLUDETAGS")
        if include_tags_value is None:
            include_tags_value = os.environ.get("ZENDESKTICKET_INCLUDETAG", "")
        exclude_tags_value = os.environ.get("ZENDESKTICKET_EXCLUDETAGS")
        if exclude_tags_value is None:
            exclude_tags_value = os.environ.get("ZENDESKTICKET_EXCLUDETAG", "")
        self._include_tags = _parse_tags(include_tags_value)
        self._exclude_tags = _parse_tags(exclude_tags_value)
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
        self._file_cache: dict[tuple[str, str], Path] = {}
        self._manifest_snapshot: dict[str, Any] = {}
        self._pending_checkpoint: datetime | None = None
        self._run_cache_dir: Path | None = None
        self._aggressive_checkpoint = _parse_bool(os.environ.get("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "false"))

    def build_manifest(self) -> list[ManifestEntry]:
        state = self._load_state()
        checkpoint = self._load_checkpoint(state)
        prior_entries = self._state_entries_by_ticket(state)
        attachments_enabled_previously = bool(state.get("attachments_enabled", False))

        self._reset_run_cache()
        current_entries_by_ticket: dict[str, list[ManifestEntry]] = {}
        current_updated_at_by_ticket: dict[str, str] = {}
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
                entries, updated_at_value = self._build_ticket_entries(ticket, comments)
                current_entries_by_ticket[ticket_id] = entries
                current_updated_at_by_ticket[ticket_id] = updated_at_value

            if page_max_updated_at is not None:
                pending_checkpoint = page_max_updated_at
                if self._aggressive_checkpoint:
                    self._save_checkpoint(pending_checkpoint)

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
                    "updated_at": current_updated_at_by_ticket.get(
                        ticket_id,
                        ((state.get("ticket_files") or {}).get(ticket_id, {})).get("updated_at", ""),
                    ),
                }
                for ticket_id, entries in combined_entries_by_ticket.items()
            },
        }
        self._pending_checkpoint = pending_checkpoint
        return manifest

    def read_file(self, path: str, filename: str) -> bytes:
        content_path = self._file_cache.get((path, filename))
        if content_path is None:
            raise FileNotFoundError(f"Ticket file not found: {path}/{filename}" if path else f"Ticket file not found: {filename}")
        return content_path.read_bytes()

    def mark_sync_complete(self) -> None:
        if self._pending_checkpoint is not None:
            self._save_checkpoint(self._pending_checkpoint)
        self._save_state(self._manifest_snapshot)

    def has_content(self) -> bool:
        return bool(self._manifest_snapshot.get("ticket_files"))

    def requires_empty_sync(self) -> bool:
        return bool(self._manifest_snapshot) and not self.has_content()

    def close(self) -> None:
        if self._run_cache_dir and self._run_cache_dir.exists():
            shutil.rmtree(self._run_cache_dir, ignore_errors=True)
        self._run_cache_dir = None
        self._file_cache.clear()
        self._http.close()

    def _build_ticket_entries(self, ticket: dict[str, Any], comments: list[dict[str, Any]]) -> tuple[list[ManifestEntry], str]:
        ticket_id = str(ticket["id"])
        updated_at_value = str(ticket.get("updated_at") or "")
        markdown = self._render_ticket_markdown(ticket, comments).encode("utf-8")
        entries = [self._cache_entry(path="", filename=f"{ticket_id}.md", content=markdown)]

        if self._download_attachments:
            attachments = self._collect_attachments(ticket, comments)
            for attachment in attachments:
                content = self._download_attachment(attachment["content_url"])
                short_hash = hashlib.sha1(content).hexdigest()[:6]  # noqa: S324
                filename = f"{ticket_id}-{short_hash}-{self._sanitize_filename(attachment['file_name'])}"
                entries.append(self._cache_entry(path=ticket_id, filename=filename, content=content))

        return entries, updated_at_value

    def _cache_entry(self, path: str, filename: str, content: bytes) -> ManifestEntry:
        if self._run_cache_dir is None:
            raise RuntimeError("Run cache directory not initialized")
        key = f"{path}/{filename}" if path else filename
        cache_file = self._run_cache_dir / hashlib.sha256(key.encode("utf-8")).hexdigest()
        cache_file.write_bytes(content)
        self._file_cache[(path, filename)] = cache_file
        return ManifestEntry(
            filename=filename,
            path=path,
            checksum=hashlib.sha256(content).hexdigest()[:16],
            size=len(content),
        )

    def _iter_ticket_pages(self, checkpoint: datetime) -> Iterator[list[dict[str, Any]]]:
        next_page: str | None = None
        while True:
            if next_page:
                response = self._zendesk_get(next_page)
            else:
                response = self._zendesk_get(
                    "/incremental/tickets.json",
                    params={
                        "per_page": self._page_size,
                        "start_time": self._format_start_time(checkpoint),
                    },
                )
            response.raise_for_status()
            payload = response.json()
            tickets = payload.get("tickets", [])
            yield tickets
            next_page = payload.get("next_page")
            if not next_page:
                break

    def _fetch_ticket_comments(self, ticket_id: int) -> list[dict[str, Any]]:
        response = self._zendesk_get(f"/tickets/{ticket_id}/comments.json")
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
        hostname = (urlparse(url).hostname or "").lower()
        zendesk_host = f"{self._subdomain}.zendesk.com".lower()
        if hostname and hostname != zendesk_host:
            return self._download_with_redirects(url)
        else:
            return self._download_with_redirects(url, client=self._http)

    def _download_with_redirects(self, url: str, client: httpx.Client | None = None) -> bytes:
        if client is not None:
            response = client.get(url)
            status_code = getattr(response, "status_code", None)
            if status_code in _ZENDESK_ATTACHMENT_REDIRECT_CODES:
                location = getattr(response, "headers", {}).get("location")
                if not location:
                    raise ValueError("Zendesk attachment redirect response did not include a location")
                response = client.get(location)
            response.raise_for_status()
            return response.content

        response = httpx.get(url, timeout=30.0)
        response.raise_for_status()
        return response.content

    def _zendesk_get(self, path: str, params: dict[str, Any] | None = None):
        if self._verbose_http:
            print(f"[zendesktickets] GET {path} params={params}")
        last_response = None
        for attempt in range(self._max_retries + 1):
            response = self._http.get(path, params=params)
            last_response = response
            status_code = getattr(response, "status_code", None)
            if status_code != _ZENDESK_RATE_LIMIT_STATUS:
                return response
            if attempt == self._max_retries:
                return response
            delay = self._retry_delay_seconds(response, attempt)
            if self._verbose_http:
                print(f"[zendesktickets] 429 retry in {delay:.2f}s (attempt {attempt + 1}/{self._max_retries})")
            time.sleep(delay)
        return last_response

    def _retry_delay_seconds(self, response: Any, attempt: int) -> float:
        retry_after = getattr(response, "headers", {}).get("retry-after")
        if retry_after:
            try:
                parsed = float(retry_after)
            except ValueError:
                parsed = 0.0
            if parsed > 0:
                return min(parsed, self._backoff_max_seconds)
        exponential_delay = self._backoff_base_seconds * (2**attempt)
        return min(exponential_delay, self._backoff_max_seconds)

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

    def _reset_run_cache(self) -> None:
        cache_dir = self._state_dir / ".run-cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._run_cache_dir = cache_dir
        self._file_cache.clear()

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


def _parse_page_size(value: str) -> int:
    try:
        page_size = int(value)
    except ValueError as exc:
        raise ValueError("ZENDESKTICKET_PAGE_SIZE must be a positive integer.") from exc
    if page_size <= 0:
        raise ValueError("ZENDESKTICKET_PAGE_SIZE must be a positive integer.")
    return page_size


def _parse_non_negative_int(value: str, var_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{var_name} must be a non-negative integer.") from exc
    if parsed < 0:
        raise ValueError(f"{var_name} must be a non-negative integer.")
    return parsed


def _parse_positive_float(value: str, var_name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{var_name} must be a positive number.") from exc
    if parsed <= 0:
        raise ValueError(f"{var_name} must be a positive number.")
    return parsed
