"""Zendesk Tickets connector skeleton."""

from __future__ import annotations

import os

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


def _parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [tag.strip() for tag in value.split(",") if tag.strip()]


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
        self._page_size = int(os.environ.get("ZENDESKTICKET_PAGE_SIZE", "100"))
        self._download_attachments = _parse_bool(os.environ.get("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS"))
        self._include_tags = _parse_tags(os.environ.get("ZENDESKTICKET_INCLUDETAGS"))
        self._exclude_tags = _parse_tags(os.environ.get("ZENDESKTICKET_EXCLUDETAGS"))
        self._state_dir = state_dir
        if not self._subdomain or not self._token:
            raise ValueError(
                "Zendesk tickets credentials required. Set ZENDESKTICKET_SUBDOMAIN and ZENDESKTICKET_TOKEN env vars."
            )
        self._http = httpx.Client(
            base_url=f"https://{self._subdomain}.zendesk.com/api/v2",
            auth=(f"{self._user}/token", self._token),
            timeout=30.0,
        )
        self._cache: dict[str, bytes] = {}
        self._manifest: list[ManifestEntry] = []

    def build_manifest(self) -> list[ManifestEntry]:
        return []

    def read_file(self, path: str, filename: str) -> bytes:
        raise FileNotFoundError(f"Ticket not found: {filename}")

    def close(self) -> None:
        self._http.close()


def parse_zendesktickets_source(source: str) -> dict[str, str | None]:
    subdomain = source.removeprefix("zendesktickets:")
    if not subdomain:
        raise ValueError("Invalid Zendesk tickets source. Expected: zendesktickets:<subdomain>")
    return {"subdomain": subdomain}
