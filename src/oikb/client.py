"""HTTP client wrapping the Open WebUI Knowledge Base sync API."""

from __future__ import annotations

import json
from typing import Any, Self

import httpx

# Safety cap for pathological servers that keep returning novel items;
# 100k pages at even 1 item/page is far beyond any real KB.  Hitting the
# cap raises rather than silently returning a possibly-partial listing.
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

    def __enter__(self) -> Self:
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
        """GET /knowledge/{id} — get KB metadata.

        Note: this endpoint returns metadata only. Its ``files`` field is a
        server-hydrated convenience that some Open WebUI versions return as
        null; use ``list_kb_files``/``count_kb_files`` for the file list.
        """
        resp = self._http.get(f"/knowledge/{kb_id}")
        resp.raise_for_status()
        return resp.json()

    def list_kb_files(
        self, kb_id: str, page_size: int | None = None
    ) -> list[dict[str, Any]]:
        """GET /knowledge/{id}/files — list every file linked to a KB.

        Paginated: walks ``page`` until the reported ``total`` is reached —
        or, when the server reports no ``total``, until a page yields no
        new files (natural exhaustion).  The listing is complete-or-raise:
        a server that stops serving new files before ``total`` is reached,
        or that exhausts the page-safety cap, raises ``ValueError`` rather
        than returning a partial list as if it were complete (#43/#46).
        ``page_size`` is passed as ``limit``, which the server only honors
        for admin keys — non-admin callers get the default 30-item page
        size and the loop simply takes more iterations.
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
            items = data.get("items")
            if items is None:
                items = []
            elif not isinstance(items, list):
                raise ValueError(
                    f"Malformed KB file listing for {kb_id}: 'items' must be a list, got {type(items).__name__}"
                )
            total = data.get("total")
            if isinstance(total, bool):
                # bool is an int subclass, so a JSON `true` total would
                # otherwise slip through the int checks below.
                raise ValueError(  # noqa: TRY004  -- repo convention: malformed API payloads raise ValueError
                    f"Malformed KB file listing for {kb_id}: 'total' must be an integer, got {total!r}"
                )
            if total is not None and not isinstance(total, int):
                total = None  # tolerate non-integer totals as absent
            new_items: list[dict[str, Any]] = []
            for f in items:
                fid = f.get("id")
                # Items without an id are kept as-is (not deduped);
                # duplicates are dropped within a page and across pages.
                if fid is None or fid not in seen_ids:
                    new_items.append(f)
                    if fid is not None:
                        seen_ids.add(fid)
            files.extend(new_items)
            if total is None:
                if not new_items:
                    break  # natural exhaustion: no total, nothing new
            elif len(files) >= total:
                break
            elif not new_items:
                raise ValueError(
                    f"KB file listing for {kb_id} stalled: page {page} returned no new files after collecting {len(files)} of {total} reported"
                )
            if page >= _KB_FILES_MAX_PAGES:
                raise ValueError(
                    f"KB file listing for {kb_id} exceeded the {_KB_FILES_MAX_PAGES}-page safety cap after collecting {len(files)} files"
                )
            page += 1
        return files

    def count_kb_files(self, kb_id: str) -> int:
        """GET /knowledge/{id}/files — total file count for a KB."""
        resp = self._http.get(f"/knowledge/{kb_id}/files")
        resp.raise_for_status()
        return resp.json().get("total", 0)
