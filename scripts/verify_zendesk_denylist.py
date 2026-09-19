#!/usr/bin/env python3
"""Read-only verifier: confirm denylisted Zendesk tickets are gone from the KB.

Purpose (issue #38)
-------------------
The connector denies tickets listed in ``ZENDESKTICKET_DENYLIST_FILES`` and
the *next normal sync* purges them from the Open WebUI knowledge base via the
deleted-diff. This script never mutates anything: it reads the denylist and
the KB listing and reports drift:

  CLEAN    every denylisted ticket is absent from the KB            (exit 0)
  LEAKED   denylisted ticket files still exist in the KB             (exit 1)
  ERROR    cannot read the denylist or reach the KB                  (exit 2)

Run it after a sync that follows a denylist change; also useful as a
periodic safety net. It does NOT delete leaked files: if LEAKED is reported,
re-run/rerun the oikb sync (the connector's carried-forward filter removes
them) — do not hand-delete KB files, which would desync manifest_state.json.

Matching: ticket files are stored as ``tickets/<id>.md`` and attachments as
``attachments/<id>/<id>-<hash>-<name>`` (see the connector's
_build_ticket_entries). A denylisted ID "leaks" when any KB file's path
matches ``tickets/<id>/<id>.md`` or ``attachments/<id>/...``.

Usage
-----
Denylist file: same plaintext format the connector consumes (one numeric
ticket ID per line, ``#`` comments).

    export OPEN_WEBUI_URL=https://openwebui.example.com
    export OPEN_WEBUI_API_KEY=<key>          # same vars oikb uses
    python scripts/verify_zendesk_denylist.py <kb_id> <denylist-file> [...]

Multiple denylist files (the union is verified):
    python scripts/verify_zendesk_denylist.py <kb_id> denylist1.txt denylist2.txt

Stdlib only; reads GET /api/v1/knowledge/{id} for the file list.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

_TIMEOUT_DEFAULT = 60.0


def _load_deny_ids(paths: list[str]) -> set[str]:
    denied: set[str] = set()
    for raw_path in paths:
        with open(raw_path, encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                if not entry.isdigit():
                    print(
                        f"error: malformed denylist line {raw_path}:{line_number}: "
                        f"expected numeric ticket ID, got {entry!r}",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                denied.add(entry)
    return denied


def _list_kb_files(base_url: str, api_key: str, kb_id: str, timeout: float) -> list[dict[str, Any]]:
    url = base_url.rstrip("/") + f"/api/v1/knowledge/{kb_id}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "oikb-deny-verify/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"error: KB request failed with HTTP {exc.code}", file=sys.stderr)
        sys.exit(2)
    except urllib.error.URLError as exc:
        print(f"error: cannot reach Open WebUI at {base_url}: {exc}", file=sys.stderr)
        sys.exit(2)
    # Null-tolerant: the KB response's "files" may be an explicit JSON null
    # (see oikb client.py list_kb_files for the same normalization).
    return payload.get("files") or []


def _leaked_files(denied: set[str], kb_files: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Map denylisted ticket ID -> KB file display paths still present."""
    leaked: dict[str, list[str]] = {}
    for file_entry in kb_files:
        path = str(file_entry.get("path") or "")
        filename = str(file_entry.get("filename") or "")
        # Connector layout: tickets/<id>/<id>.md and attachments/<id>/<file>.
        parts = path.split("/")
        ticket_id: str | None = None
        if parts == ["tickets"]:
            stem = filename.rsplit(".", 1)[0] if "." in filename else filename
            if stem in denied:
                ticket_id = stem
        elif len(parts) == 2 and parts[0] == "attachments" and parts[1] in denied:
            ticket_id = parts[1]
        if ticket_id is not None:
            leaked.setdefault(ticket_id, []).append(f"{path}/{filename}" if path else filename)
    return leaked


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
    timeout = float(os.environ.get("VERIFY_TIMEOUT_SECONDS") or _TIMEOUT_DEFAULT)

    try:
        denied = _load_deny_ids(denylist_paths)
    except OSError as exc:
        print(f"error: cannot read denylist: {exc}", file=sys.stderr)
        sys.exit(2)
    if not denied:
        print("Denylist is empty; nothing to verify.")
        return

    kb_files = _list_kb_files(base_url, api_key, kb_id, timeout)
    leaked = _leaked_files(denied, kb_files)

    print(f"Denylisted ticket IDs: {len(denied)} (from {', '.join(denylist_paths)})")
    print(f"KB files listed:       {len(kb_files)}")
    if not leaked:
        print("\nVERDICT: CLEAN — no denylisted ticket has files in the KB. (exit 0)")
        return
    print(f"\nVERDICT: LEAKED — {len(leaked)} denylisted ticket(s) still have KB files: (exit 1)")
    for ticket_id in sorted(leaked, key=lambda v: int(v)):
        print(f"  ticket {ticket_id}:")
        for display in leaked[ticket_id]:
            print(f"    - {display}")
    print(
        "\nDo NOT hand-delete these: run the oikb sync (the connector's "
        "carried-forward denylist filter purges them and keeps "
        "manifest_state.json consistent)."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()