#!/usr/bin/env python3
"""Smoke test: does GET /api/v2/tickets/show_many.json serve ARCHIVED tickets?

Background
----------
The planned zendesktickets LLM scanner (legacy-ticket denylist builder) needs
to walk ticket IDs in strict creation order (1..STOP_TICKET_ID).  The
candidate mechanism is ``tickets/show_many.json`` (up to 100 IDs per call).
That plan depends on show_many returning tickets Zendesk has ARCHIVED:
archived legacy tickets are the entire population the scanner must classify,
they can no longer be edited (so they cannot be given the ``ach_request`` /
``sensitive`` tags), and Zendesk's behavior on serving archived tickets
through the standard endpoints is exactly what this script measures
empirically rather than trusting docs or memory.

What it checks, per ticket ID passed (default: 45748, a known archived
legacy ticket):
  1. GET /tickets/{id}.json -- cross-reference via the single-fetch path.
  2. GET /tickets/show_many.json?ids=... -- the batch path the scanner
     wants to use; IDs that come back omitted (HTTP 200 but absent from
     the payload) are the archive-blindness signal.
  3. GET /users/show_many.json?ids=... -- whether requester_id values on
     returned tickets still resolve to email addresses (the scanner uses
     sender emails as classification signal).

Usage
-----
Uses the same env vars as the oikb zendesktickets connector:

    export ZENDESKTICKET_SUBDOMAIN=porky
    export ZENDESKTICKET_USER=you@example.com   # email only; /token added here
    export ZENDESKTICKET_TOKEN=...               # Zendesk API token

    python scripts/zendesk_archive_smoke.py                  # default: 45748
    python scripts/zendesk_archive_smoke.py 45748 45749 12345
    python scripts/zendesk_archive_smoke.py 45748 --raw      # dump full JSON

Keep the ID list <= 100 (the show_many page limit).  Add a mix of known-
archived legacy IDs and one known-recent ID to see both behaviors.

Exit codes
----------
  0  show_many returned every requested ID (single GET status irrelevant)
  2  show_many omitted at least one ID that single GET found -> archive-
     blind; the scanner must cross-check every show_many omission with a
     single GET /tickets/{id}.json before treating the ID as missing
  3  requested IDs missing from BOTH paths (deleted / never existed?)
  1  transport failure or bad credentials; nothing concluded

Stdlib only; no backend imports, runs anywhere Python 3.9+ runs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_DEFAULT_SUBDOMAIN = "porky"
_DEFAULT_TICKET_ID = "45748"
_TIMEOUT_SECONDS = 30.0
_MAX_SHOW_MANY = 100


def _auth_header(user: str, token: str) -> str:
    raw = f"{user}/token:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _get(base_url: str, path_qs: str, auth: str, timeout: float) -> tuple[int, Any, str]:
    url = base_url + path_qs
    request = urllib.request.Request(
        url,
        headers={"Authorization": auth, "User-Agent": "oikb-archive-smoke/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"  error: request to {url} failed: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = None  # non-JSON body; raw text is still reported
    return status, payload, raw


def _snippet(value: Any, limit: int = 70) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _print_ticket(id_label: str, ticket: dict[str, Any], via: str) -> None:
    print(f"    {via}: FOUND")
    print(f"      subject:     {_snippet(ticket.get('subject'))!r}")
    print(f"      created_at:  {ticket.get('created_at')}")
    print(f"      updated_at:  {ticket.get('updated_at')}")
    print(f"      status:      {ticket.get('status')}")
    print(f"      tags:        {ticket.get('tags') or []}")
    print(f"      requester:   {ticket.get('requester_id')}")
    print(f"      fields:      {sorted(ticket.keys())}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Check whether Zendesk tickets/show_many.json returns archived "
            "tickets, and whether single fetch and users/show_many.json "
            "agree. Defaults to the known archived legacy ticket 45748."
        ),
    )
    default_subdomain = os.environ.get("ZENDESKTICKET_SUBDOMAIN") or _DEFAULT_SUBDOMAIN
    parser.add_argument(
        "ticket_ids",
        nargs="*",
        default=[],
        help=f"Ticket IDs to probe (default: {_DEFAULT_TICKET_ID}).",
    )
    parser.add_argument(
        "--subdomain",
        default=default_subdomain,
        help=f"Zendesk subdomain (default: $ZENDESKTICKET_SUBDOMAIN or {default_subdomain!r}).",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Dump the full JSON of the first ticket found via show_many.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_TIMEOUT_SECONDS,
        help=f"Per-request timeout in seconds (default: {_TIMEOUT_SECONDS:g}).",
    )
    args = parser.parse_args()

    subdomain = args.subdomain
    user = os.environ.get("ZENDESKTICKET_USER", "")
    token = os.environ.get("ZENDESKTICKET_TOKEN", "")
    if not user or not token:
        print(
            "error: set ZENDESKTICKET_USER (email) and ZENDESKTICKET_TOKEN "
            "(API token); optionally ZENDESKTICKET_SUBDOMAIN.",
            file=sys.stderr,
        )
        sys.exit(1)

    id_args = args.ticket_ids or [_DEFAULT_TICKET_ID]
    ids = [part.strip() for a in id_args for part in a.split(",") if part.strip()]
    for value in ids:
        if not value.isdigit():
            print(f"error: ticket IDs must be numeric, got {value!r}", file=sys.stderr)
            sys.exit(1)
    if len(ids) > _MAX_SHOW_MANY:
        print(f"error: at most {_MAX_SHOW_MANY} IDs per show_many call", file=sys.stderr)
        sys.exit(1)
    if len(set(ids)) != len(ids):
        print("error: duplicate ticket IDs given", file=sys.stderr)
        sys.exit(1)

    timeout = args.timeout
    base_url = f"https://{subdomain}.zendesk.com/api/v2"
    auth = _auth_header(user, token)

    print(f"Zendesk: https://{subdomain}.zendesk.com  (user: {user})")
    print(f"Probing {len(ids)} ticket ID(s): {', '.join(ids)}")
    print()

    # ---- 1. show_many (the batch path the scanner wants to use) ---------
    qs = urllib.parse.urlencode({"ids": ",".join(ids)})
    show_many_status, payload, raw = _get(base_url, f"/tickets/show_many.json?{qs}", auth, timeout)
    status = show_many_status
    print(f"[batch] GET /tickets/show_many.json?ids=<n={len(ids)}> -> HTTP {status}")
    if status != 200:
        print(f"  body: {_snippet(raw, 300)}")
        print("  fatal: show_many did not answer 200; nothing concluded.", file=sys.stderr)
        sys.exit(1)
    if not isinstance(payload, dict) or not isinstance(payload.get("tickets"), list):
        print(f"  body: {_snippet(raw, 300)}")
        print(
            "  fatal: show_many answered 200 with a malformed payload "
            "(expected a JSON object with a list-valued 'tickets' field); nothing concluded.",
            file=sys.stderr,
        )
        sys.exit(1)
    show_many: dict[str, dict[str, Any]] = {}
    for ticket in payload["tickets"]:
        if isinstance(ticket, dict) and "id" in ticket:
            show_many[str(ticket["id"])] = ticket
    print(
        f"  payload count={payload.get('count')} "
        f"returned={len(show_many)} requested={len(ids)}"
    )

    # ---- 2. per-ID single fetch (cross-reference) -----------------------
    by_single: dict[str, dict[str, Any]] = {}
    for ticket_id in ids:
        status, payload, raw = _get(base_url, f"/tickets/{ticket_id}.json", auth, timeout)
        print(f"[single] GET /tickets/{ticket_id}.json -> HTTP {status}")
        if status == 200 and isinstance(payload, dict) and isinstance(payload.get("ticket"), dict):
            by_single[ticket_id] = payload["ticket"]
        elif status == 200:
            print(f"  body: {_snippet(raw, 200)}")
        else:
            print(f"  body: {_snippet(raw, 200)}")

    # ---- Ticket detail report ------------------------------------------
    print()
    for ticket_id in ids:
        print(f"ticket {ticket_id}:")
        if ticket_id in show_many:
            _print_ticket(ticket_id, show_many[ticket_id], via="show_many")
        else:
            print(
                "    show_many: OMITTED (HTTP 200, not in payload)"
                if show_many_status == 200
                else f"    show_many: not checked (HTTP {show_many_status})"
            )
        if ticket_id in by_single:
            _print_ticket(ticket_id, by_single[ticket_id], via="single GET")
        else:
            print("    single GET: NOT FOUND")
        print()

    # ---- 3. requester user resolution -----------------------------------
    # show_many is authoritative for shared IDs; single-GET-only tickets
    # (show_many omissions) must still contribute their requesters.
    found = {**by_single, **show_many}
    requester_ids = sorted(
        {
            str(ticket.get("requester_id"))
            for ticket in found.values()
            if ticket.get("requester_id")
        }
    )
    resolved: dict[str, dict[str, Any]] = {}
    if requester_ids:
        qs = urllib.parse.urlencode({"ids": ",".join(requester_ids)})
        status, payload, raw = _get(base_url, f"/users/show_many.json?{qs}", auth, timeout)
        print(f"[users] GET /users/show_many.json?ids=<n={len(requester_ids)}> -> HTTP {status}")
        if status == 200:
            if isinstance(payload, dict) and isinstance(payload.get("users"), list):
                for entry in payload["users"]:
                    if isinstance(entry, dict) and "id" in entry:
                        resolved[str(entry["id"])] = entry
            else:
                print(f"  body: {_snippet(raw, 200)}")
        else:
            print(f"  body: {_snippet(raw, 200)}")
    for rid in requester_ids:
        entry = resolved.get(rid)
        if entry is None:
            print(f"  requester {rid}: OMITTED by users/show_many")
        else:
            print(
                f"  requester {rid}: {entry.get('email')} "
                f"(name={entry.get('name')!r}, active={entry.get('active')})"
            )

    if args.raw and found:
        first_id = next(iter(found))
        print()
        print(f"---- raw JSON: first found ticket ({first_id}) ----")
        print(json.dumps(found[first_id], indent=2, sort_keys=True))

    # ---- Verdict ---------------------------------------------------------
    show_many_hits = sum(1 for i in ids if i in show_many)
    single_hits = sum(1 for i in ids if i in by_single)
    print()
    print(f"Summary: show_many {show_many_hits}/{len(ids)}, single GET {single_hits}/{len(ids)}")
    if show_many_hits == len(ids):
        print(
            "VERDICT: show_many returned every requested ID — archived "
            "tickets ARE served; the scanner can walk IDs 1..STOP_ID via "
            "show_many batches. (exit 0)"
        )
        sys.exit(0)
    if single_hits > show_many_hits:
        missing = [i for i in ids if i not in show_many and i in by_single]
        print(
            f"VERDICT: show_many omitted {len(missing)} ID(s) that single GET "
            f"found ({', '.join(missing)}) — show_many appears archive-blind; "
            "the scanner must cross-check every show_many omission with a "
            "single GET /tickets/{id}.json before treating the ID as missing. "
            "(exit 2)"
        )
        sys.exit(2)
    missing = [i for i in ids if i not in show_many and i not in by_single]
    print(
        f"VERDICT: {len(missing)} ID(s) missing from BOTH paths "
        f"({', '.join(missing)}) — deleted or nonexistent tickets; "
        "re-run with IDs known to exist. (exit 3)"
    )
    sys.exit(3)


if __name__ == "__main__":
    main()