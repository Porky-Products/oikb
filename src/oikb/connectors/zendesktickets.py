"""Zendesk Tickets connector skeleton."""

from __future__ import annotations

import os

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
        self._state_dir = state_dir
        if not self._subdomain or not self._user or not self._token:
            raise ValueError(
                "Zendesk tickets credentials required. Set ZENDESKTICKET_SUBDOMAIN, ZENDESKTICKET_USER, and ZENDESKTICKET_TOKEN env vars."
            )
        self._http = httpx.Client(
            base_url=f"https://{self._subdomain}.zendesk.com/api/v2",
            auth=(f"{self._user}/token", self._token),
            timeout=30.0,
        )

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
