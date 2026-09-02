"""Zendesk Tickets connector — sync tickets and attachments to a Knowledge Base."""

from __future__ import annotations

import hashlib
import json
import logging
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

log = logging.getLogger(__name__)

_ZENDESK_ATTACHMENT_REDIRECT_CODES = {301, 302, 303, 307, 308}
_ZENDESK_RATE_LIMIT_STATUS = 429
_TICKET_PATH = "tickets"
_ATTACHMENT_PATH = "attachments"


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
        status_value = os.environ.get("ZENDESKTICKET_STATUS", "")
        self._include_tags = _parse_tags(include_tags_value)
        self._exclude_tags = _parse_tags(exclude_tags_value)
        self._statuses = _parse_statuses(status_value)
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
        self._manifest_entries_by_key: dict[tuple[str, str], ManifestEntry] = {}
        self._restored_content: dict[tuple[str, str], bytes] = {}
        self._pending_checkpoint: datetime | None = None
        self._run_cache_dir: Path | None = None
        self._aggressive_checkpoint = _parse_bool(os.environ.get("ZENDESKTICKET_AGGRESSIVE_CHECKPOINT", "false"))
        max_tickets_value = os.environ.get("ZENDESKTICKET_MAX_TICKETS_PER_RUN", "1000")
        self._max_tickets_per_run: int | None = _parse_optional_positive_int(max_tickets_value, "ZENDESKTICKET_MAX_TICKETS_PER_RUN")

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
        tickets_processed = 0
        cap_reached = False
        page_end_time_seen = False

        for page_tickets, page_end_time in self._iter_ticket_pages(checkpoint):
            page_max_updated_at: datetime | None = None
            for ticket in page_tickets:
                ticket_id = str(ticket["id"])
                updated_at = self._parse_dt(ticket["updated_at"])
                page_max_updated_at = updated_at if page_max_updated_at is None else max(page_max_updated_at, updated_at)
                seen_ticket_ids.add(ticket_id)

                if not self._should_include_ticket(ticket):
                    excluded_ticket_ids.add(ticket_id)
                    continue

                comments = self._fetch_ticket_comments(ticket["id"])
                if comments is None:
                    excluded_ticket_ids.add(ticket_id)
                    continue
                entries, updated_at_value = self._build_ticket_entries(ticket, comments)
                current_entries_by_ticket[ticket_id] = entries
                current_updated_at_by_ticket[ticket_id] = updated_at_value
                tickets_processed += 1

                if self._max_tickets_per_run is not None and tickets_processed >= self._max_tickets_per_run:
                    # Finish the current page before stopping: the checkpoint
                    # for a capped run is the page's end_time (Zendesk's own
                    # cursor). Stopping mid-page would either reuse this
                    # page's end_time and re-serve the unprocessed remainder
                    # of the page or skip it, and any updated_at-derived
                    # value can jump past tickets that still need processing
                    # (Zendesk compares start_time against
                    # generated_timestamp, not updated_at). Overshoot is
                    # bounded by per_page - 1 tickets.
                    cap_reached = True

            # Zendesk's documented resume strategy for time-based exports is
            # to use the returned end_time (the next_page URL start_time) as
            # the next export's start_time. end_time is the generated_timestamp
            # of the last item on the page — the API's own forward cursor —
            # and results are ordered by generated_timestamp, so it is
            # gap-free AND stall-free even when pages carry out-of-order
            # updated_at values (updated_at reflects ticket events only, while
            # generated_timestamp reflects all updates).
            #
            # The FIRST end_time observed in the run replaces the pending
            # checkpoint outright rather than max-comparing against it: a
            # prior legacy page may have set a fallback from updated_at, and
            # updated_at can sit beyond the true cursor (it does not bound
            # generated_timestamp). Keeping such a fallback would advance the
            # checkpoint past records that were never served, and once
            # persisted the no-regress guard locks it in — the exact gap
            # mode this change eliminates. After the first end_time, only
            # end_time values participate; later end_time values normally
            # increase (pages ordered by generated_timestamp) but are still
            # max-compared defensively. The updated_at fallback exists solely
            # for runs whose pages lack end_time entirely, where stalling
            # would be worse; fallback values are never aggressively saved
            # mid-run — a crash re-serves tickets (safe), whereas persisting
            # a beyond-cursor fallback skips them (unsafe).
            if page_end_time is not None:
                if not page_end_time_seen or page_end_time > pending_checkpoint:
                    page_end_time_seen = True
                    pending_checkpoint = page_end_time
                    if self._aggressive_checkpoint:
                        self._save_checkpoint(page_end_time)
                # else: end_time cursor already at or past this page's value.
            elif page_max_updated_at is not None and not page_end_time_seen:
                if pending_checkpoint is None or page_max_updated_at > pending_checkpoint:
                    # No end_time observed yet this run: retain
                    # max-updated_at so out-of-order legacy pages cannot
                    # stall the checkpoint.
                    pending_checkpoint = page_max_updated_at

            if cap_reached:
                break

        carried_forward = {
            ticket_id: entries
            for ticket_id, entries in prior_entries.items()
            if ticket_id not in seen_ticket_ids and ticket_id not in excluded_ticket_ids
        }

        if attachments_enabled_previously and not self._download_attachments:
            for ticket_id, entries in list(carried_forward.items()):
                attachment_path = f"{_ATTACHMENT_PATH}/{ticket_id}"
                carried_forward[ticket_id] = [
                    entry
                    for entry in entries
                    if entry.path != attachment_path
                ]

        combined_entries_by_ticket = carried_forward | current_entries_by_ticket
        manifest = [entry for entries in combined_entries_by_ticket.values() for entry in entries]
        manifest.sort(key=lambda entry: entry.display_path)

        self._manifest_entries_by_key = {(entry.path, entry.filename): entry for entry in manifest}
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
        checkpoint_advanced = pending_checkpoint > checkpoint
        log.info(
            "ZendeskTicketsConnector.run_summary: checkpoint_in=%s checkpoint_out=%s advanced=%s "
            "tickets_seen=%d tickets_processed=%d cap_reached=%s manifest_entries=%d "
            "carried_forward_tickets=%d new_tickets=%d",
            self._format_dt(checkpoint),
            self._format_dt(pending_checkpoint) if pending_checkpoint is not None else "none",
            checkpoint_advanced,
            len(seen_ticket_ids),
            tickets_processed,
            cap_reached,
            len(manifest),
            len(carried_forward),
            len(current_entries_by_ticket),
        )
        if cap_reached and pending_checkpoint is not None and pending_checkpoint == checkpoint:
            # The run cap stopped inside the very page the run started on and
            # even its end_time did not move the cursor — every ticket
            # including the boundary shares one generated_timestamp (bulk
            # import). An inclusive >= boundary would refetch them forever.
            log.warning(
                "ZendeskTicketsConnector.checkpoint_stalled: cap reached without advancing checkpoint "
                "(in=%s out=%s seen=%d processed=%d) — check for duplicate "
                "generated_timestamp values at the inclusive start_time boundary",
                self._format_dt(checkpoint),
                self._format_dt(pending_checkpoint),
                len(seen_ticket_ids),
                tickets_processed,
            )
        return manifest

    def read_file(self, path: str, filename: str) -> bytes:
        content_path = self._file_cache.get((path, filename))
        if content_path is not None:
            return content_path.read_bytes()
        cached = self._restored_content.get((path, filename))
        if cached is not None:
            return cached
        content = self._restore_from_run_cache(path, filename)
        if content is None:
            raise FileNotFoundError(f"Ticket file not found: {path}/{filename}" if path else f"Ticket file not found: {filename}")
        return content

    def _restore_from_run_cache(self, path: str, filename: str) -> bytes | None:
        """Fall back to a leftover .run-cache file from a prior run.

        Carried-forward manifest entries never pass through _cache_entry(), so
        _file_cache has no record of them. With aggressive checkpointing the
        .run-cache directory persists across runs, allowing the bytes to be
        restored here instead of failing the upload.
        """
        if self._run_cache_dir is None:
            return None
        expected = self._manifest_entries_by_key.get((path, filename))
        if expected is None:
            return None
        key = f"{path}/{filename}" if path else filename
        cache_file = self._run_cache_dir / hashlib.sha256(key.encode("utf-8")).hexdigest()
        try:
            if not cache_file.is_file():
                return None
            size = cache_file.stat().st_size
        except OSError as e:
            log.warning("Run-cache stat failed for %s/%s: %s", path, filename, e)
            return None
        if size != expected.size:
            return None
        try:
            content = cache_file.read_bytes()
        except OSError as e:
            log.warning("Run-cache read failed for %s/%s: %s", path, filename, e)
            return None
        if hashlib.sha256(content).hexdigest()[:16] != expected.checksum:
            return None
        self._restored_content[(path, filename)] = content
        return content

    def mark_sync_complete(self) -> None:
        log.info(
            "ZendeskTicketsConnector.mark_sync_complete: saving checkpoint=%s state_tickets=%d",
            self._format_dt(self._pending_checkpoint) if self._pending_checkpoint is not None else "none",
            len(self._manifest_snapshot.get("ticket_files") or {}),
        )
        if self._pending_checkpoint is not None:
            self._save_checkpoint(self._pending_checkpoint)
        self._save_state(self._manifest_snapshot)

    def has_content(self) -> bool:
        return bool(self._manifest_snapshot.get("ticket_files"))

    def requires_empty_sync(self) -> bool:
        return bool(self._manifest_snapshot) and not self.has_content()

    def close(self) -> None:
        preserve = self._aggressive_checkpoint and self._checkpoint_path().exists()
        if not preserve and self._run_cache_dir and self._run_cache_dir.exists():
            shutil.rmtree(self._run_cache_dir, ignore_errors=True)
        self._run_cache_dir = None
        self._file_cache.clear()
        self._http.close()

    def _build_ticket_entries(self, ticket: dict[str, Any], comments: list[dict[str, Any]]) -> tuple[list[ManifestEntry], str]:
        ticket_id = str(ticket["id"])
        updated_at_value = str(ticket.get("updated_at") or "")
        markdown = self._render_ticket_markdown(ticket, comments).encode("utf-8")
        entries = [self._cache_entry(path=_TICKET_PATH, filename=f"{ticket_id}.md", content=markdown)]

        if self._download_attachments:
            attachments = self._collect_attachments(ticket, comments)
            for attachment in attachments:
                content = self._download_attachment_with_retry(attachment["content_url"])
                if content is None:
                    if self._verbose_http:
                        print(f"[zendesktickets] skipping attachment after retries: {attachment['content_url']}")
                    continue
                short_hash = hashlib.sha1(content).hexdigest()[:6]  # noqa: S324
                filename = f"{ticket_id}-{short_hash}-{self._sanitize_filename(attachment['file_name'])}"
                entries.append(self._cache_entry(path=f"{_ATTACHMENT_PATH}/{ticket_id}", filename=filename, content=content))

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

    def _iter_ticket_pages(self, checkpoint: datetime) -> Iterator[tuple[list[dict[str, Any]], datetime | None]]:
        next_page: str | None = None
        seen_page_urls: set[str] = set()
        page_count = 0
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
            page_count += 1
            page_max = max(
                (self._parse_dt(t["updated_at"]) for t in tickets if t.get("updated_at")),
                default=None,
            )
            log.info(
                "ZendeskTicketsConnector.page: page=%d tickets=%d page_max_updated_at=%s end_of_stream=%s end_time=%s",
                page_count,
                len(tickets),
                self._format_dt(page_max) if page_max else "none",
                bool(payload.get("end_of_stream")),
                payload.get("end_time", "none"),
            )
            end_time_value = payload.get("end_time")
            if end_time_value is None:
                end_time = None
            else:
                # Zendesk returns end_time as an epoch-seconds integer.
                try:
                    end_time = datetime.fromtimestamp(int(end_time_value), tz=UTC)
                except (TypeError, ValueError):
                    log.warning(
                        "ZendeskTicketsConnector.bad_end_time: value=%s",
                        end_time_value,
                    )
                    end_time = None
            yield tickets, end_time

            # Zendesk incremental API signals "caught up" via end_of_stream.
            # Always check this before following next_page — when end_of_stream
            # is true, next_page still exists but points back to the same cursor,
            # which would cause an infinite loop.
            if payload.get("end_of_stream"):
                break

            next_page = payload.get("next_page")
            if not next_page:
                break

            # Guard: if we have already fetched this exact URL the cursor has
            # not advanced and continuing would loop forever.
            if next_page in seen_page_urls:
                break
            seen_page_urls.add(next_page)

    def _fetch_ticket_comments(self, ticket_id: int) -> list[dict[str, Any]] | None:
        """Fetch comments for a ticket, returning None on 404 or after exhausting retries."""
        for attempt in range(self._max_retries + 1):
            response = self._zendesk_get(f"/tickets/{ticket_id}/comments.json")
            status = getattr(response, "status_code", None)
            if status == 404:
                if self._verbose_http:
                    print(f"[zendesktickets] skipping inaccessible ticket {ticket_id} (404)")
                return None
            if status == _ZENDESK_RATE_LIMIT_STATUS or (status is not None and status >= 500):
                if attempt == self._max_retries:
                    if self._verbose_http:
                        print(f"[zendesktickets] skipping ticket {ticket_id} after retries (status {status})")
                    return None
                delay = self._retry_delay_seconds(response, attempt)
                if self._verbose_http:
                    print(f"[zendesktickets] retry ticket {ticket_id} in {delay:.2f}s (attempt {attempt + 1}/{self._max_retries})")
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json().get("comments", [])
        return None

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

    def _download_attachment_with_retry(self, url: str) -> bytes | None:
        retries = 3
        for attempt in range(retries + 1):
            try:
                return self._download_attachment(url)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (404,) or status == 429 or status >= 500:
                    if attempt == retries:
                        return None
                    time.sleep(30 * (attempt + 1))
                    continue
                raise
            except httpx.TransportError:
                if attempt == retries:
                    raise
                time.sleep(30 * (attempt + 1))
        return None

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
        if self._statuses:
            status = str(ticket.get("status") or "").strip().lower()
            if status not in self._statuses:
                return False
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
        # Guard against checkpoint regression: Zendesk pages can arrive out of
        # updated_at order, so never persist a value older than what is on
        # disk. Otherwise a buggy or unlucky run would rewind the resume point.
        if path.exists():
            existing = self._parse_dt(path.read_text().strip())
            if value <= existing:
                log.info(
                    "ZendeskTicketsConnector.checkpoint_not_saved: incoming=%s existing=%s (no advance)",
                    self._format_dt(value),
                    self._format_dt(existing),
                )
                return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._format_dt(value))
        log.info("ZendeskTicketsConnector.checkpoint_saved: path=%s value=%s", path, self._format_dt(value))

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
        if self._aggressive_checkpoint and self._checkpoint_path().exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._run_cache_dir = cache_dir
            self._file_cache.clear()
            return
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


def _parse_statuses(value: str) -> set[str]:
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


def _parse_optional_positive_int(value: str, var_name: str) -> int | None:
    """Parse a positive integer, returning None if the value is '0' or empty (meaning no cap)."""
    stripped = value.strip()
    if not stripped or stripped == "0":
        return None
    try:
        parsed = int(stripped)
    except ValueError as exc:
        raise ValueError(f"{var_name} must be a positive integer or 0 to disable.") from exc
    if parsed < 0:
        raise ValueError(f"{var_name} must be a positive integer or 0 to disable.")
    return parsed
