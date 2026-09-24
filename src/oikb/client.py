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
        new files (natural exhaustion).  The first non-null ``total`` is
        binding for the whole listing: a later page that changes or omits
        it raises.  The listing is complete-or-raise: a server that stops
        serving new files before ``total`` is reached, or that exhausts the
        page-safety cap, raises ``ValueError`` rather than returning a
        partial list as if it were complete (#43/#46).  Entries must carry
        non-empty string ids; duplicates are dropped within a page and
        across pages.  ``page_size`` is passed as ``limit``, which the
        server only honors for admin keys — non-admin callers get the
        default 30-item page size and the loop simply takes more iterations.
        """
        files: list[dict[str, Any]] = []
        seen_entries: dict[str, dict[str, Any]] = {}
        reported_total: int | None = None
        page = 1
        while True:
            params: dict[str, Any] = {"page": page}
            if page_size is not None:
                params["limit"] = page_size
            resp = self._http.get(f"/knowledge/{kb_id}/files", params=params)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                # A non-object body (JSON list/string/null) is malformed:
                # coercing it to {} would return an empty list as if it
                # were the complete listing.
                raise ValueError(  # noqa: TRY004 -- repo convention: malformed API payloads raise ValueError
                    f"Malformed KB file listing for {kb_id}: response body must be a JSON object, got {type(data).__name__}"
                )
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
            if total is not None and (
                isinstance(total, bool) or not isinstance(total, int) or total < 0
            ):
                # Fail closed on a malformed total: bool is an int subclass
                # (JSON `true` would pass isinstance), a non-integer total
                # must not silently swap complete-or-raise for lenient
                # natural exhaustion, and a negative total would satisfy
                # `len(files) >= total` on page 1 and return a partial
                # listing as complete.
                raise ValueError(
                    f"Malformed KB file listing for {kb_id}: 'total' must be a non-negative integer, got {total!r}"
                )
            if total is not None:
                if reported_total is None:
                    reported_total = total
                elif total != reported_total:
                    # A total that changes mid-pagination is a moving
                    # target: the pages held so far may no longer be the
                    # complete set, and honoring the new (smaller) total
                    # would return a partial listing as complete.
                    raise ValueError(
                        f"Malformed KB file listing for {kb_id}: 'total' changed mid-pagination from {reported_total!r} to {total!r} on page {page}"
                    )
            elif reported_total is not None:
                # A total that disappears mid-pagination would downgrade
                # complete-or-raise to lenient natural exhaustion.
                raise ValueError(
                    f"Malformed KB file listing for {kb_id}: 'total' disappeared on page {page} after {reported_total!r} was reported"
                )
            new_items: list[dict[str, Any]] = []
            for f in items:
                if not isinstance(f, dict):
                    raise ValueError(  # noqa: TRY004 -- repo convention: malformed API payloads raise ValueError
                        f"Malformed KB file listing for {kb_id}: entry must be a JSON object, got {type(f).__name__}"
                    )
                fid = f.get("id")
                # Every entry must carry a usable string id: an id-less
                # entry cannot be deduped yet still counts toward
                # ``total``, so duplicates of it can satisfy
                # ``len(files) >= total`` and return a partial listing as
                # complete; a non-string id breaks the set[str] dedup
                # contract (and an unhashable one would raise an
                # incidental TypeError instead of the documented
                # ValueError).
                if not isinstance(fid, str) or not fid:
                    raise ValueError(
                        f"Malformed KB file listing for {kb_id}: entry id must be a non-empty string, got {fid!r}"
                    )
                # Identical duplicates (overlapping pages serving the same
                # entry) are deduplicated within a page and across pages. A
                # conflicting copy of an id already held means the listing
                # shifted while being read: returning either copy as a
                # complete listing could hand callers stale metadata (e.g. a
                # pre-shift hash, undermining the duplicate-upload guard),
                # so complete-or-raise fails closed here.
                existing = seen_entries.get(fid)
                if existing is None:
                    new_items.append(f)
                    seen_entries[fid] = f
                elif existing != f:
                    raise ValueError(
                        f"Malformed KB file listing for {kb_id}: conflicting duplicate entry for id {fid!r} on page {page}"
                    )
            files.extend(new_items)
            if reported_total is None:
                if not new_items:
                    break  # natural exhaustion: no total, nothing new
            elif len(files) > reported_total:
                # More unique files than the binding total is internally
                # inconsistent metadata: the total cannot be trusted to
                # mark the listing complete, and later pages may still
                # hold files. Complete-or-raise means raise here.
                raise ValueError(
                    f"Malformed KB file listing for {kb_id}: collected {len(files)} unique files exceeding the reported total of {reported_total} on page {page}"
                )
            elif len(files) == reported_total:
                break
            elif not new_items:
                raise ValueError(
                    f"KB file listing for {kb_id} stalled: page {page} returned no new files after collecting {len(files)} of {reported_total} reported"
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
