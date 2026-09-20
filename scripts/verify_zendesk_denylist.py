#!/usr/bin/env python3
"""Read-only verifier: confirm denylisted Zendesk tickets are gone from the KB.

Purpose (issue #38)
-------------------
The connector denies tickets listed in ``ZENDESKTICKET_DENYLIST_FILES`` and
the *next normal sync* purges them from the Open WebUI knowledge base via the
deleted-diff. This script never mutates anything: it reads the denylist and
the KB file listing and reports drift.

Which endpoint and why
----------------------
GET /api/v1/knowledge/{id} does NOT work for this: its ``files`` entries are
FileMetadataResponse objects ({id, hash, meta, created_at, updated_at}) with
no filename, and this deployment has been observed to return an explicit
``"files": null`` (see oikb tests/test_null_tolerance.py). Matching against
that shape would always report CLEAN.

The verifier therefore uses GET /api/v1/knowledge/{id}/files (open-webui
get_knowledge_files_by_id -> KnowledgeFileListResponse: items are
FileUserResponse objects carrying ``filename``, plus id/hash/meta; ``total``
is the full count). Pagination is the 1-based ``page`` query parameter with
the server's PAGE_ITEM_COUNT per page (30 by default).

Matching
--------
The connector uploads ticket docs as ``<id>.md`` (KB directory "tickets")
and attachments as ``<id>-<hash>-<name>`` (KB directory "attachments/<id>").
Endpoint items do not carry their directory path, so the verifier matches on
filename alone: a denylisted ID leaks if any item's filename is exactly
``<id>.md`` or starts with ``<id>-``. The trailing separator binds the
numeric prefix (45748- cannot false-match 457480-).

Verdicts
--------
  CLEAN   exit 0 — full KB listing retrieved and no denylisted match
  LEAKED  exit 1 — denylisted ticket files still exist in the KB
  ERROR   exit 2 — unreadable denylist, unreachable/invalid KB response, or
          an INDETERMINATE listing: an empty/zero-total payload is not
          evidence of purge (wrong kb id, credentials, or a KB that never
          synced zendesktickets)

Run after a sync that follows a denylist change; also useful as a periodic
safety net. It does NOT delete leaked files: if LEAKED is reported, re-run
the oikb sync (the connector's carried-forward filter purges them) — do not
hand-delete KB files, which would desync manifest_state.json.

Usage
-----
    export OPEN_WEBUI_URL=https://openwebui.example.com
    export OPEN_WEBUI_API_KEY=<key>          # same vars oikb uses
    python scripts/verify_zendesk_denylist.py <kb_id> <denylist-file> [...]
"""

from __future__ import annotations

import http.client
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_TIMEOUT_DEFAULT = 60.0
_MAX_PAGES = 10000  # hard stop against a misbehaving server


def _die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(2)


def _load_deny_ids(paths: list[str]) -> set[str]:
    denied: set[str] = set()
    for raw_path in paths:
        try:
            with open(raw_path, encoding="utf-8") as fh:
                for line_number, line in enumerate(fh, start=1):
                    entry = line.strip()
                    if not entry or entry.startswith("#"):
                        continue
                    # ASCII-only + canonicalized, matching the connector's
                    # _load_denylist_files exactly: non-ASCII digits pass
                    # str.isdigit() but never match an ASCII ticket id, and
                    # raw storage would let '045748' silently match nothing.
                    if not (entry.isascii() and entry.isdigit()):
                        _die(
                            f"malformed denylist line {raw_path}:{line_number}: "
                            f"expected a numeric ticket ID, got {entry!r}"
                        )
                    denied.add(str(int(entry)))
        except UnicodeDecodeError as exc:
            # Not an OSError subclass: without this, a non-UTF-8 denylist
            # would crash with an uncaught traceback exiting 1 — the code
            # documented as LEAKED — instead of a clean ERROR exit 2.
            _die(f"denylist file {raw_path!r} is not valid UTF-8: {exc}")
    return denied


def _list_kb_files(base_url: str, api_key: str, kb_id: str, timeout: float) -> list[dict[str, Any]]:
    """Page through GET /knowledge/{id}/files and return every item.

    Exits 2 on failure or an indeterminate (empty/zero-total/partial)
    listing: only a complete, non-empty listing can support a CLEAN verdict.
    """
    items: list[dict[str, Any]] = []
    seen_total: int | None = None
    for page in range(1, _MAX_PAGES + 1):
        qs = urllib.parse.urlencode({"page": page})
        url = (
            base_url.rstrip("/")
            + f"/api/v1/knowledge/{urllib.parse.quote(str(kb_id))}/files?{qs}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "oikb-deny-verify/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            _die(f"KB request failed with HTTP {exc.code}: {url}")
        except urllib.error.URLError as exc:
            _die(f"cannot reach Open WebUI at {base_url}: {exc}")
        except json.JSONDecodeError as exc:
            _die(f"KB response was not JSON: {exc}")
        except (TimeoutError, http.client.HTTPException, OSError, UnicodeDecodeError) as exc:
            # Post-connect failures that are NOT urllib.error.URLError
            # subclasses (read-phase stall -> TimeoutError, truncated body ->
            # IncompleteRead, decode issues): these are infrastructure errors
            # and MUST route to exit 2 — exit 1 is reserved for a confirmed
            # leak (LEAKED), and a traceback exit(1) here would read as one.
            _die(f"KB response failed or was unreadable: {type(exc).__name__}: {exc}")
        if not isinstance(payload, dict):
            _die(f"KB response was not a JSON object: {str(payload)[:200]}")
        # Null-tolerant parse (this server has served explicit nulls).
        page_items = payload.get("items") or []
        total = payload.get("total")
        if not isinstance(page_items, list):
            _die(f"KB response 'items' was not a list: {type(page_items).__name__}")
        if not isinstance(total, int) or total < 0:
            _die(f"KB response 'total' missing/invalid: {total!r}")
        items.extend(entry for entry in page_items if isinstance(entry, dict))
        seen_total = total
        if len(items) >= total:
            break
    if seen_total is None or seen_total == 0 or not items:
        _die(
            "INDETERMINATE: KB file listing is empty (total="
            f"{seen_total!r}). An empty listing is not evidence that "
            "denylisted tickets were purged — check the kb id, credentials, "
            "and that this KB actually synced the zendesktickets source."
        )
    if len(items) < (seen_total or 0):
        _die(
            f"listing incomplete: fetched {len(items)} of {seen_total} files; "
            "refusing to report CLEAN on a partial listing"
        )
    return items


def _items_from_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Null-tolerant item extraction shared with tests."""
    page_items = payload.get("items") or []
    return [entry for entry in page_items if isinstance(entry, dict)]


def _item_filename(item: dict[str, Any]) -> str:
    """Best filename for a KB file item, mirroring oikb's own display logic:
    meta.name is the upload filename; filename is the FileModelResponse field."""
    meta = item.get("meta") or {}
    name = meta.get("name") if isinstance(meta, dict) else None
    return str(name or item.get("filename") or "")


def _leaked_files(denied: set[str], items: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Map denylisted ticket ID -> KB item filenames still present."""
    leaked: dict[str, list[str]] = {}
    for item in items:
        filename = _item_filename(item)
        if not filename:
            continue
        for ticket_id in denied:
            if filename == f"{ticket_id}.md" or filename.startswith(f"{ticket_id}-"):
                leaked.setdefault(ticket_id, []).append(filename)
    return leaked


def _auth_header(api_key: str) -> str:
    return "Bearer " + api_key


def main() -> None:
    args = [a for a in sys.argv[1:] if a and not a.startswith("--")]
    if len(args) < 2:
        print(
            "usage: verify_zendesk_denylist.py <kb_id> <denylist-file> [more-denylist-files...]",
            file=sys.stderr,
        )
        sys.exit(2)
    kb_id, denylist_paths = args[0], args[1:]

    base_url = os.environ.get("OPEN_WEBUI_URL")
    api_key = os.environ.get("OPEN_WEBUI_API_KEY")
    if not base_url or not api_key:
        print(
            "error: set OPEN_WEBUI_URL and OPEN_WEBUI_API_KEY (same variables oikb uses)",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        timeout = float(os.environ.get("VERIFY_TIMEOUT_SECONDS") or _TIMEOUT_DEFAULT)
    except ValueError as exc:
        # A bad timeout is an operator configuration error: it must exit 2
        # (ERROR), never 1 — the status reserved for a confirmed leak.
        _die(f"invalid VERIFY_TIMEOUT_SECONDS: {exc}")
    # float() accepts nan/inf/-1/0; urlopen would later raise uncaught
    # ValueError/OverflowError/URLError exiting 1 (the LEAKED code).
    if not math.isfinite(timeout) or timeout <= 0:
        _die(f"invalid VERIFY_TIMEOUT_SECONDS: must be a positive finite number (got {timeout!r})")

    try:
        denied = _load_deny_ids(denylist_paths)
    except OSError as exc:
        _die(f"cannot read denylist: {exc}")
    if not denied:
        print("Denylist is empty; nothing to verify.")
        return

    items = _list_kb_files(base_url, api_key, kb_id, timeout)
    leaked = _leaked_files(denied, items)

    print(f"Denylisted ticket IDs: {len(denied)} (from {', '.join(denylist_paths)})")
    print(f"KB files listed:       {len(items)}")
    if not leaked:
        print("\nVERDICT: CLEAN — no denylisted ticket has files in the KB. (exit 0)")
        return
    print(f"\nVERDICT: LEAKED — {len(leaked)} denylisted ticket(s) still have KB files: (exit 1)")
    for ticket_id in sorted(leaked, key=lambda v: int(v)):
        print(f"  ticket {ticket_id}:")
        for filename in leaked[ticket_id]:
            print(f"    - {filename}")
    print(
        "\nDo NOT hand-delete these: run the oikb sync (the connector's "
        "carried-forward denylist filter purges them and keeps "
        "manifest_state.json consistent)."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
