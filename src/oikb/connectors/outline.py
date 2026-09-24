"""Outline connector -- sync documents from an Outline wiki."""

from __future__ import annotations

import hashlib
import os

import httpx

from oikb.connectors import BaseConnector, ManifestEntry

# documents.list serves at most 100 documents per request. Unlike the
# zendesktickets _MAX_COMMENT_PAGES guard, which bounds comments within a
# single ticket, this is a workspace-wide listing, so the cap must tolerate
# large workspaces: 10,000 pages bounds a sync at 1,000,000 documents,
# matching the whole-listing hard stop in verify_zendesk_denylist.py. A
# server that keeps returning full pages of new documents forever is
# pathological -- fail closed instead of looping endlessly.
_MAX_PAGES = 10_000


class OutlineConnector(BaseConnector):
    """Sync documents from Outline."""

    def __init__(self, collection: str | None = None, token: str | None = None, base_url: str | None = None):
        self._token = token or os.environ.get("OUTLINE_TOKEN")
        self._base = base_url or os.environ.get("OUTLINE_URL", "https://app.getoutline.com")
        if not self._token:
            raise ValueError("Set OUTLINE_TOKEN env var.")
        self._collection = collection
        self._http = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            timeout=30.0,
        )
        self._cache: dict[str, str] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        # Resolve collection ID once before starting the loop
        collection_id = None
        if self._collection:
            # Walk every page of collections.list: a lookup limited to the
            # first page would misreport a real collection as missing.
            cols: list[dict] = []
            offset = 0
            for _ in range(_MAX_PAGES):
                cols_resp = self._http.post(
                    "/api/collections.list", json={"offset": offset, "limit": 100}
                )
                cols_resp.raise_for_status()
                cols_payload = cols_resp.json()
                if not isinstance(cols_payload, dict) or not isinstance(cols_payload.get("data"), list):
                    raise ValueError(  # noqa: TRY004 -- repo convention: malformed API payloads raise ValueError
                        "Outline collections.list returned a malformed payload: "
                        "expected a JSON object with a list-valued 'data' field"
                    )
                page_cols = cols_payload["data"]
                cols.extend(c for c in page_cols if isinstance(c, dict))
                if len(page_cols) < 100:
                    break
                offset += 100
            else:
                raise ValueError(
                    f"Outline collections.list exceeded {_MAX_PAGES} pages without completing; "
                    "aborting to avoid an endless pagination loop"
                )
            col = next(
                (c for c in cols if c.get("name") == self._collection or c.get("id") == self._collection),
                None,
            )
            if col:
                collection_id = col["id"]
            else:
                # A configured collection that cannot be resolved must fail
                # before listing documents: leaving collection_id None makes
                # documents.list omit collectionId and silently sync the
                # entire workspace instead of the requested scope.
                raise ValueError(
                    f"Outline collection {self._collection!r} not found; "
                    "refusing to fall back to a workspace-wide sync"
                )

        entries: list[ManifestEntry] = []
        seen: set[str] = set()
        offset = 0
        limit = 100  # The maximum allowed by the server per request
        pages = 0

        while True:
            pages += 1
            # Use 'offset' instead of 'page' as per the API spec
            params: dict = {
                "offset": offset,
                "limit": limit,
                "sort": "updatedAt",
                "direction": "DESC"
            }
            if collection_id:
                params["collectionId"] = collection_id

            resp = self._http.post("/api/documents.list", json=params)
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                # A malformed page (non-object body, or data missing/null/
                # non-list) must not be read as natural exhaustion: that
                # would return the partial manifest as complete and hide
                # the remaining documents. Fail closed instead.
                raise ValueError(
                    "Outline documents.list returned a malformed payload: "
                    "expected a JSON object with a list-valued 'data' field"
                )
            docs = payload["data"]

            if not docs:
                break

            if pages > _MAX_PAGES:
                # The one request beyond the page budget exists only so a
                # listing whose length is an exact multiple of the page size
                # can confirm completion with an EMPTY page (handled above).
                # A non-empty page past the budget means the listing exceeds
                # the cap: abort rather than process it and return an
                # over-budget manifest as if complete.
                raise ValueError(
                    f"Outline documents.list exceeded {_MAX_PAGES} pages without completing; "
                    "aborting to avoid an endless pagination loop"
                )

            added = 0
            for doc in docs:
                doc_id = doc.get("id")
                if not doc_id:
                    # Fail closed: without an id the file can neither be named
                    # nor deduplicated, and silently skipping would hide content.
                    raise ValueError(f"Outline document is missing an id: {doc.get('title', 'untitled')!r}")
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                title = doc.get("title", "untitled")
                text = doc.get("text", "")
                content = f"# {title}\n\n{text}"
                filename = f"{doc_id}.md"
                checksum = hashlib.sha256(content.encode()).hexdigest()[:16]
                entries.append(ManifestEntry(filename=filename, path="", checksum=checksum, size=len(content.encode())))
                self._cache[filename] = content
                added += 1

            if not added:
                raise ValueError(
                    f"Outline pagination made no progress at offset {offset}: "
                    "the page returned only already-seen documents"
                )

            # If we received fewer than the limit, we've reached the end of the list
            if len(docs) < limit:
                break

            # Increment offset by the number of items retrieved to get the next batch
            offset += limit

        return entries

    def read_file(self, path: str, filename: str) -> bytes:
        return (self._cache.get(filename) or "").encode("utf-8")

    def close(self) -> None:
        self._http.close()


def parse_outline_source(source: str) -> dict[str, str | None]:
    collection = source.removeprefix("outline:") or None
    return {"collection": collection}
