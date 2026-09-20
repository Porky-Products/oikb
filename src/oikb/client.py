"""HTTP client wrapping the Open WebUI Knowledge Base sync API."""

from __future__ import annotations

import json
from typing import Any

import httpx

# Safety cap for pathological servers that keep returning novel items;
# 100k pages at even 1 item/page is far beyond any real KB.
_KB_FILES_MAX_PAGES = 100_000


class OikbClient:
    """Stateless HTTP client for the Open WebUI KB API.

    All methods are synchronous — httpx handles connection pooling internally.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 120.0):
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=f"{self._base_url}/api/v1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def __enter__(self) -> OikbClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ── Sync API ────────────────────────────────────────────────

    def sync_diff(
        self,
        kb_id: str,
        manifest: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """POST /knowledge/{id}/sync/diff — compute diff from manifest."""
        resp = self._http.post(
            f"/knowledge/{kb_id}/sync/diff",
            json={"manifest": manifest},
        )
        resp.raise_for_status()
        return resp.json()

    def sync_cleanup(
        self,
        kb_id: str,
        file_ids: list[str],
        dir_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /knowledge/{id}/sync/cleanup — remove stale files and dirs."""
        payload: dict[str, Any] = {"file_ids": file_ids}
        if dir_ids:
            payload["dir_ids"] = dir_ids
        resp = self._http.post(
            f"/knowledge/{kb_id}/sync/cleanup",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    # ── File upload ─────────────────────────────────────────────

    def upload_file(
        self,
        file_content: bytes,
        filename: str,
        kb_id: str,
        file_hash: str,
        directory_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /files/ — upload a single file to the KB."""

        metadata: dict[str, Any] = {
            "knowledge_id": kb_id,
            "file_hash": file_hash,
        }
        if directory_id:
            metadata["directory_id"] = directory_id

        resp = self._http.post(
            "/files/",
            files={"file": (filename, file_content)},
            data={"metadata": json.dumps(metadata)},
        )
        resp.raise_for_status()
        return resp.json()

    # ── Directory management ────────────────────────────────────

    def create_directory(
        self,
        kb_id: str,
        name: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /knowledge/{id}/dirs/create — create a directory."""
        payload: dict[str, Any] = {"name": name}
        if parent_id:
            payload["parent_id"] = parent_id
        resp = self._http.post(
            f"/knowledge/{kb_id}/dirs/create",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    # ── KB management ───────────────────────────────────────────

    def reset_kb(
        self,
        kb_id: str,
        include_directories: bool = True,
    ) -> dict[str, Any]:
        """POST /knowledge/{id}/reset — reset the KB."""
        resp = self._http.post(
            f"/knowledge/{kb_id}/reset",
            params={"include_directories": include_directories},
        )
        resp.raise_for_status()
        return resp.json()

    def get_kb(self, kb_id: str) -> dict[str, Any]:
        """GET /knowledge/{id} — get KB info."""
        resp = self._http.get(f"/knowledge/{kb_id}")
        resp.raise_for_status()
        return resp.json()

    def list_kb_files(
        self, kb_id: str, page_size: int | None = None
    ) -> list[dict[str, Any]]:
        """GET /knowledge/{id}/files — list every file linked to a KB.

        Paginated: walks ``page`` until the reported ``total`` is reached
        or a page yields nothing new.  ``page_size`` is passed as ``limit``,
        which the server only honors for admin keys — non-admin callers
        get the default 30-item page size and the loop simply takes more
        iterations.
        """
        files: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        page = 1
        while True:
            params: dict[str, Any] = {"page": page}
            if page_size is not None:
                params["limit"] = page_size
            resp = self._http.get(f"/knowledge/{kb_id}/files", params=params)
            resp.raise_for_status()
            data = resp.json() or {}
            # "items" may be an explicit JSON null — .get's default only
            # covers a missing key, not a null value.
            items = data.get("items") or []
            new_items = [
                f for f in items if f.get("id") is None or f["id"] not in seen_ids
            ]
            if not new_items:
                break  # empty page or a repeated page — no progress
            for f in new_items:
                if f.get("id") is not None:
                    seen_ids.add(f["id"])
            files.extend(new_items)
            total = data.get("total")
            if isinstance(total, int) and len(files) >= total:
                break
            if page >= _KB_FILES_MAX_PAGES:
                break
            page += 1
        return files
