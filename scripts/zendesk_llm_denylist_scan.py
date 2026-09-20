#!/usr/bin/env python3
"""Build a ticket denylist by classifying legacy Zendesk tickets with an LLM.

Purpose (see issue #38)
-----------------------
Legacy archived tickets (e.g. 45748) contain credit/ACH application data but
pre-date the ``ach_request`` / ``sensitive`` tag conventions, and archived
tickets can no longer be tagged. This script walks ticket IDs 1..STOP_ID in
strict creation order and asks an OpenAI-compatible LLM, per ticket, whether
it matches the deny criteria described in an operator-provided prompt file.
Verdicts:

  deny   -> appended to the denylist file (one numeric ID per line)
  unsure -> appended to the review file for human triage
  allow  -> nothing written

The denylist file is consumed by the oikb connector via
``ZENDESKTICKET_DENYLIST_FILES``; denylisted tickets are purged from the KB by
the next normal sync run.

Why ID order and not dates: ticket IDs are creation-ordered and immutable, so
``id <= STOP_ID`` (the ID where Zendesk auto-tagging rules landed) exactly
bounds the affected population. A date cutoff on the incremental stream
(generated_timestamp order, based on updated_at) would mis-bound tickets
created early but commented on later.

Why show_many: verified live 2026-09-19 (scripts/zendesk_archive_smoke.py)
that GET /tickets/show_many.json serves ARCHIVED tickets (2/2, incl. 45748)
and users/show_many.json resolves requester emails.

Fail-closed policy
------------------
* LLM infra failure (timeout / 5xx / auth) after retries -> ABORT (nonzero);
  resume is free via the statefile cursor, so never guess under outage.
* Malformed LLM response after retries -> verdict falls to ``unsure`` and the
  ticket goes to the review file, never silently allow.
* Truncated/oversized ticket -> forced ``unsure`` (don't classify away the
  evidence).
* Missing env vars -> abort before any Zendesk call.

No numeric confidence: verdicts are categorical (deny/unsure/allow) only.

Statefile (JSON): {"next_id", "stop_id", "prompt_sha256", "stats", "saved_at"}.
Resume continues from next_id. Changing the prompt file between runs is
detected via prompt_sha256 and aborts (classification policy changed; restart
with --reset or keep prompts stable across a full pass).

Env vars
--------
Zendesk (same names as the connector):
  ZENDESKTICKET_SUBDOMAIN          required
  ZENDESKTICKET_USER               required (email; /token appended)
  ZENDESKTICKET_TOKEN              required

Scanner:
  LLM_SCAN_STOP_TICKET_ID          required; classify IDs 1..STOP_ID inclusive
  LLM_SCAN_PROMPT_FILE             required; what-to-match prompt (no format
                                   instructions -- this script owns the
                                   response format and appends them)
  LLM_SCAN_DENYLIST_FILE           required; denylist to create/append
  LLM_SCAN_REVIEW_FILE             required; unsure verdicts land here
  LLM_SCAN_STATE_FILE              required; resume cursor
  OPENAI_API_KEY                   required; sent as Bearer
  OPENAI_BASE_URL                  required; e.g. https://api.openai.com/v1
  OPENAI_MODEL                     required; model name
  LLM_SCAN_MAX_PER_RUN             optional; default 1000, 0 = unbounded
  LLM_SCAN_TIMEOUT_SECONDS         optional; default 120 per HTTP call
  LLM_SCAN_MAX_LLM_RETRIES        optional; default 3 per ticket
  LLM_SCAN_DESC_CHAR_CAP           optional; default 6000 chars of description
                                   per ticket; larger -> forced unsure

Stdlib only; no backend imports, runs anywhere Python 3.9+ runs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BATCH_IDS = 100  # Zendesk show_many page limit
_DESC_CAP_DEFAULT = 6000
_TIMEOUT_DEFAULT = 120.0
_MAX_LLM_RETRIES_DEFAULT = 3
_MAX_PER_RUN_DEFAULT = 1000

REQUIRED_ENV = (
    "ZENDESKTICKET_SUBDOMAIN",
    "ZENDESKTICKET_USER",
    "ZENDESKTICKET_TOKEN",
    "LLM_SCAN_STOP_TICKET_ID",
    "LLM_SCAN_PROMPT_FILE",
    "LLM_SCAN_DENYLIST_FILE",
    "LLM_SCAN_REVIEW_FILE",
    "LLM_SCAN_STATE_FILE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
)

# Appended after the operator prompt: the strict response contract. The
# operator prompt states WHAT to match; this block states HOW to answer.
_FORMAT_INSTRUCTIONS = """\

Return ONLY a JSON object (no prose, no code fences) with exactly these keys:
{
  "ticket_id": <integer, the ticket ID you examined>,
  "verdict": "deny" | "unsure" | "allow"
}

Rules for the verdict:
- "deny": the ticket clearly matches the deny criteria described above.
- "unsure": you cannot confidently classify (ambiguous, truncated, or
  missing context). Prefer "unsure" over a guess in either direction.
- "allow": the ticket clearly does NOT match the deny criteria.
- Do not report confidence or any other fields; the verdict is categorical."""


def _die(message: str) -> "None":
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def _env_int(name: str, default: int, minimum: int) -> int:
    """Parse a non-negative-int env var, _die()ing with a clear message on
    anything else. Bare int() would either traceback (non-numeric) or, for
    negatives like MAX_PER_RUN=-1, silently short-circuit the scan loop and
    exit 0 as a 'completed pass' that classified zero tickets."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip()
    if not (value.isascii() and value.isdigit()) :
        _die(f"{name} must be an integer >= {minimum} (got {raw!r})")
    parsed = int(value)
    if parsed < minimum:
        _die(f"{name} must be an integer >= {minimum} (got {raw!r})")
    return parsed


def _env_float(name: str, default: float, minimum: float) -> float:
    """Parse a positive-float env var; _die() on non-numeric, negative, or
    non-finite values (float() alone accepts nan/inf, which urlopen later
    rejects with an uncaught ValueError/OverflowError)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw.strip())
    except ValueError as exc:
        _die(f"{name} must be a number >= {minimum} (got {raw!r}): {exc}")
    if not math.isfinite(parsed) or parsed < minimum:
        _die(f"{name} must be a finite number >= {minimum} (got {raw!r})")
    return parsed


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None,
    body: bytes | None = None,
    timeout: float,
) -> tuple[int, Any, str]:
    request = urllib.request.Request(url, data=body, headers=headers or {})
    if method != "GET":
        request.get_method = lambda: method  # type: ignore[assignment]
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = None
    return status, payload, raw


class ZendeskClient:
    """Minimal read-only Zendesk v2 client with 429/backoff handling
    mirroring the connector's _zendesk_get semantics (bounded retries,
    Retry-After honored, exponential backoff)."""

    def __init__(self, subdomain: str, user: str, token: str, timeout: float, max_retries: int):
        self._base = f"https://{subdomain}.zendesk.com/api/v2"
        raw = f"{user}/token:{token}".encode("utf-8")
        self._auth = "Basic " + base64.b64encode(raw).decode("ascii")
        self._timeout = timeout
        self._max_retries = max_retries

    def _get(self, path_qs: str) -> Any:
        url = self._base + path_qs
        delay = 1.0
        for attempt in range(self._max_retries + 1):
            status, payload, raw = _http_json(
                url, headers={"Authorization": self._auth, "User-Agent": "oikb-llm-scan/1"}, timeout=self._timeout
            )
            if status != 429:
                return status, payload, raw
            if attempt == self._max_retries:
                return status, payload, raw
            # Retry-After: honor when parseable, else exponential backoff.
            pause = delay
            time.sleep(pause)
            delay = min(delay * 2, 90.0)
        return status, payload, raw  # pragma: no cover - unreachable

    def show_many_tickets(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        qs = urllib.parse.urlencode({"ids": ",".join(str(i) for i in ids)})
        status, payload, _ = self._get(f"/tickets/show_many.json?{qs}")
        if status != 200:
            raise RuntimeError(f"Zendesk show_many HTTP {status} for {len(ids)} ids")
        out: dict[int, dict[str, Any]] = {}
        for ticket in (payload or {}).get("tickets") or []:
            if isinstance(ticket, dict) and isinstance(ticket.get("id"), int):
                out[ticket["id"]] = ticket
        return out

    def fetch_ticket_comments(self, ticket_id: int) -> tuple[list[dict[str, Any]], bool]:
        """Fetch a ticket's comments (attachments live on comment objects).

        Returns (comments, more_pages). more_pages is True when the response
        indicates continuation: Zendesk paginates this endpoint, and this
        scanner deliberately reads only the first page — the caller forces
        `unsure` (human review) rather than classifying on partial evidence.
        Full traversal is tracked in issue #41.
        """
        status, payload, _ = self._get(f"/tickets/{ticket_id}/comments.json")
        if status == 404:
            return [], False  # ticket vanished mid-scan; classify on core fields
        if status != 200:
            raise RuntimeError(f"Zendesk comments HTTP {status} for ticket {ticket_id}")
        comments = (payload or {}).get("comments") or []
        more = bool((payload or {}).get("next_page"))
        return [c for c in comments if isinstance(c, dict)], more

    def show_many_users(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        ids = [i for i in ids if i]
        if not ids:
            return {}
        qs = urllib.parse.urlencode({"ids": ",".join(str(i) for i in ids)})
        status, payload, _ = self._get(f"/users/show_many.json?{qs}")
        if status != 200:
            raise RuntimeError(f"Zendesk users show_many HTTP {status}")
        out: dict[int, dict[str, Any]] = {}
        for user in (payload or {}).get("users") or []:
            if isinstance(user, dict) and isinstance(user.get("id"), int):
                out[user["id"]] = user
        return out


class LLMRequestError(RuntimeError):
    """Chat-completions request failed after retries (HTTP auth/outage/5xx).

    Distinct from a malformed-but-successful 2xx completion payload: request
    failures are infrastructure faults that must abort the scan rather than
    be recorded as per-ticket `unsure` verdicts (which would consume the
    ticket range without classification).
    """


class MalformedCompletionError(RuntimeError):
    """2xx completion whose JSON body is unusable (empty/malformed choices).

    This is the model misbehaving, not the transport: the ticket must fall
    back to per-ticket `unsure` for human review and the scan continues,
    per the documented fail-closed policy. Deliberately NOT an
    LLMRequestError so the outer abort handler treats it differently.
    """


class LLMClient:
    """Minimal OpenAI-compatible chat-completions client (Bearer auth)."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float, max_retries: int):
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries

    def classify(self, user_content: str) -> str:
        """Send one classification request; return the raw response text.

        Raises RuntimeError on non-2xx after bounded retries (caller aborts).
        """
        request_body = json.dumps(
            {
                "model": self._model,
                "temperature": 0,
                "messages": [
                    {
                        "role": "user",
                        "content": user_content,
                    }
                ],
            }
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "oikb-llm-scan/1",
        }
        delay = 5.0
        last_error = ""
        for attempt in range(self._max_retries + 1):
            status, payload, raw = _http_json(
                self._url, method="POST", headers=headers, body=request_body, timeout=self._timeout
            )
            if 200 <= status < 300:
                try:
                    choices = (payload or {}).get("choices") or []
                    if not choices:
                        raise MalformedCompletionError("empty choices")
                    try:
                        return str(choices[0].get("message", {}).get("content", ""))
                    except (AttributeError, TypeError) as exc:
                        raise MalformedCompletionError(f"malformed completion payload: {exc}") from exc
                except MalformedCompletionError:
                    raise
                except (AttributeError, TypeError) as exc:
                    # payload itself not a dict / not subscriptable
                    raise MalformedCompletionError(f"malformed completion payload: {exc}") from exc
            last_error = f"HTTP {status}: {raw[:300]}"
            if attempt == self._max_retries:
                break
            # 429/5xx retried; 4xx (auth, bad request) not -- abort fast.
            if status == 429 or status >= 500:
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            break
        raise LLMRequestError(f"LLM request failed after {self._max_retries + 1} attempts: {last_error}")


def _parse_verdict(text: str, expected_ticket_id: int) -> tuple[str, str]:
    """Extract and validate a verdict from raw LLM output.

    Returns (verdict, reason-for-fallback). verdict is one of deny/unsure/allow.
    Any structural problem (non-JSON, missing keys, wrong ticket_id, unknown
    verdict) resolves to ("unsure", reason) -- never allow.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        for fence in ("```json", "```"):
            if cleaned.startswith(fence):
                cleaned = cleaned[len(fence):]
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")]
        cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return "unsure", f"non-JSON response: {exc}"
    if not isinstance(parsed, dict):
        return "unsure", "response is not a JSON object"
    ticket_id = parsed.get("ticket_id")
    if isinstance(ticket_id, str) and ticket_id.strip().isdigit():
        ticket_id = int(ticket_id)
    if ticket_id != expected_ticket_id:
        return "unsure", f"ticket_id mismatch: got {ticket_id!r}, expected {expected_ticket_id}"
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in {"deny", "unsure", "allow"}:
        return "unsure", f"unknown verdict: {verdict!r}"
    return verdict, ""


def _format_ticket_block(ticket: dict[str, Any], comments: list[dict[str, Any]], requester_email: str, desc_cap: int) -> tuple[str, list[str], bool]:
    """Render one ticket for the LLM. Returns (block_text, attachment_names, truncated).

    Attachment names come from the ticket's own attachment list plus every
    comment's attachments — the same union the oikb connector builds
    (src/oikb/connectors/zendesktickets.py _collect_attachments). Ticket
    objects from show_many carry NO attachments themselves (Zendesk OAS
    TicketObject has only the boolean allow_attachments); attachment payloads
    live on comment objects, which the caller fetches via
    /tickets/{id}/comments.json.
    """
    description = str(ticket.get("description") or "")
    truncated = len(description) > desc_cap
    if truncated:
        description = description[:desc_cap] + "\n[…description truncated…]"
    attachment_names: list[str] = []
    for attachment in ticket.get("attachments") or []:
        name = str(attachment.get("file_name") or "").strip()
        if name:
            attachment_names.append(name)
    for comment in comments or []:
        for attachment in comment.get("attachments") or []:
            name = str(attachment.get("file_name") or "").strip()
            if name:
                attachment_names.append(name)
    lines = [
        f"ticket_id: {ticket.get('id')}",
        f"subject: {ticket.get('subject')!r}",
        f"created_at: {ticket.get('created_at')}",
        f"status: {ticket.get('status')}",
        f"type: {ticket.get('type')}",
        f"tags: {ticket.get('tags') or []}",
        f"requester_email: {requester_email or '(unresolved)'}",
        f"attachment_names: {attachment_names}",
        "description:",
        description or "(empty)",
    ]
    return "\n".join(lines), attachment_names, truncated


def _append_line(path: Path, line: str) -> None:
    """Append one line, inserting a separator newline first when the file's
    final line lacks one.

    Hand-edited files (the documented workflow in docs/denylist.md) may not
    end in a newline; a plain append would fuse the new ID onto the last
    entry ('46023' + '90001' -> '4602390001'), which every downstream loader
    (scanner, connector) accepts silently — fail-open for the very ticket
    being denied. Opening in 'rb' to inspect the final byte avoids
    decode/encode round-trip surprises.
    """
    needs_separator = False
    if path.exists() and path.stat().st_size > 0:
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            needs_separator = fh.read(1) != b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if needs_separator:
            fh.write("\n")
        fh.write(line)


def _append_dedup(path: Path, ticket_id: int, deny_ids: set[int]) -> None:
    if ticket_id in deny_ids:
        return
    _append_line(path, f"{ticket_id}\n")
    deny_ids.add(ticket_id)


def _append_review(path: Path, ticket_id: int, reason: str, reviewed: set[int]) -> None:
    if ticket_id in reviewed:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _append_line(path, f"{ticket_id}  # unsure: {reason}  ({stamp})\n")
    reviewed.add(ticket_id)


def _load_state(state_path: Path) -> dict[str, Any] | None:
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _die(f"state file corrupt ({state_path}): {exc}; fix or delete it to restart from ID 1")
    return None


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    payload = dict(state)
    payload["saved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, state_path)


def main() -> None:
    # ---- Required env --------------------------------------------------
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        _die(
            "missing required env vars: " + ", ".join(missing)
            + "\nSee this script's docstring for the full list and semantics."
        )
    for name in ("LLM_SCAN_STOP_TICKET_ID",):
        stop_id_raw = os.environ.get(name, "").strip()
        if not (stop_id_raw.isascii() and stop_id_raw.isdigit()) or stop_id_raw == "0" or stop_id_raw.lstrip("0") == "":
            _die(f"{name} must be a positive integer (got {os.environ.get(name)!r})")
    # canonicalize leading zeros so state comparisons are stable
    stop_id = int(os.environ["LLM_SCAN_STOP_TICKET_ID"])
    prompt_path = Path(os.environ["LLM_SCAN_PROMPT_FILE"])
    denylist_path = Path(os.environ["LLM_SCAN_DENYLIST_FILE"])
    review_path = Path(os.environ["LLM_SCAN_REVIEW_FILE"])
    state_path = Path(os.environ["LLM_SCAN_STATE_FILE"])
    max_per_run = _env_int("LLM_SCAN_MAX_PER_RUN", _MAX_PER_RUN_DEFAULT, minimum=0)
    timeout = _env_float("LLM_SCAN_TIMEOUT_SECONDS", _TIMEOUT_DEFAULT, minimum=0.1)
    llm_retries = _env_int("LLM_SCAN_MAX_LLM_RETRIES", _MAX_LLM_RETRIES_DEFAULT, minimum=0)
    desc_cap = _env_int("LLM_SCAN_DESC_CHAR_CAP", _DESC_CAP_DEFAULT, minimum=1)

    if not prompt_path.is_file():
        _die(f"prompt file not found: {prompt_path}")
    prompt_text = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt_text:
        _die(f"prompt file is empty: {prompt_path}")
    prompt_sha = _sha256_file(prompt_path)

    # ---- State / resume -------------------------------------------------
    # --reset intentionally accepts a changed prompt file or stop ID: it is
    # the documented remedy for exactly those mismatches, so the guards must
    # let it through rather than _die before the reset block can run.
    reset_requested = "--reset" in sys.argv[1:]
    state = _load_state(state_path)
    if state is not None and not reset_requested:
        if state.get("prompt_sha256") not in (None, prompt_sha):
            _die(
                "prompt file changed since the state file was written "
                "(classification policy must stay constant within one pass); "
                "use --reset to restart from ID 1, or keep the prompt file unchanged"
            )
        if int(state.get("stop_id") or 0) != stop_id:
            _die(
                "LLM_SCAN_STOP_TICKET_ID changed since the state file was written "
                f"(state has {state.get('stop_id')!r}, env has {stop_id}); use --reset to accept the new bound"
            )
    if state is not None and not reset_requested:
        next_id = int(state.get("next_id") or 1)
        stats = dict(state.get("stats") or {})
        print(f"Resuming from ID {next_id} (saved stats: {stats})")
    else:
        next_id = 1
        stats = {}
    if reset_requested:
        # Archive stale classification artifacts rather than silently keep
        # applying them: a deny decision from the *previous* prompt survives
        # into the new pass otherwise (denylist is dedup-only, never
        # retracted). Renaming to a .pre-reset-<sha8> sidecar preserves the
        # operator's history while guaranteeing the fresh pass starts from
        # empty outputs. Mid-run aborts restore nothing (operator can
        # re-merge manually from the archives if wanted).
        for path in (denylist_path, review_path):
            if path.exists() and path.stat().st_size > 0:
                archive = path.with_name(f"{path.name}.pre-reset-{prompt_sha[:8]}")
                n = 1
                while archive.exists():
                    archive = path.with_name(f"{path.name}.pre-reset-{prompt_sha[:8]}.{n}")
                    n += 1
                path.rename(archive)
                print(f"--reset: archived prior {path.name} to {archive.name}")
        if state_path.exists():
            state_path.unlink()
        print("--reset: restarting from ID 1 (prior counts discarded; outputs start empty)")
    if next_id > stop_id:
        print(f"Nothing to do: next_id={next_id} > stop_id={stop_id} (pass complete).")
        _write_completion(state_path, next_id, stop_id, prompt_sha, stats)
        return

    # ---- Existing denylist (dedup on append) ---------------------------
    deny_ids: set[int] = set()
    if denylist_path.exists():
        for line_no, line in enumerate(denylist_path.read_text(encoding="utf-8").splitlines(), start=1):
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            # Same strict rule as the connector's loader: ASCII digits only,
            # so the scanner never emits or accepts entries the connector
            # would later reject (e.g. '+45748', fullwidth digits).
            if not (entry.isascii() and entry.isdigit()):
                _die(
                    f"{denylist_path}:{line_no}: malformed denylist entry {entry!r}; "
                    "expected a plain numeric ticket ID"
                )
            deny_ids.add(int(entry))
    reviewed: set[int] = set()
    if review_path.exists():
        for line_no, line in enumerate(review_path.read_text(encoding="utf-8").splitlines(), start=1):
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            head = entry.split("#", 1)[0].strip()
            if head:
                if not (head.isascii() and head.isdigit()):
                    _die(
                        f"{review_path}:{line_no}: malformed review entry {head!r}; "
                        "expected a plain numeric ticket ID before the comment"
                    )
                reviewed.add(int(head))

    zendesk = ZendeskClient(
        subdomain=os.environ["ZENDESKTICKET_SUBDOMAIN"],
        user=os.environ["ZENDESKTICKET_USER"],
        token=os.environ["ZENDESKTICKET_TOKEN"],
        timeout=timeout,
        max_retries=llm_retries,
    )
    llm = LLMClient(
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ["OPENAI_MODEL"],
        timeout=timeout,
        max_retries=llm_retries,
    )

    classified_this_run = 0
    # Last ID this run actually processed (classified or confirmed missing).
    # The persisted cursor derives from it, never from batch_ids[-1]: a cap
    # hit mid-batch leaves the batch tail unvisited, and advancing past it
    # would silently never scan those IDs (fail-open for a denylist).
    last_processed_id: int | None = None

    print(
        f"Scanning tickets 1..{stop_id} starting at ID {next_id} "
        f"(cap {max_per_run or 'unlimited'}/run, model {os.environ['OPENAI_MODEL']}, "
        f"prompt {prompt_path.name}@{prompt_sha[:8]})"
    )

    try:
        while next_id <= stop_id and (max_per_run == 0 or classified_this_run < max_per_run):
            batch_ids = [i for i in range(next_id, min(next_id + _BATCH_IDS - 1, stop_id) + 1)]
            tickets = zendesk.show_many_tickets(batch_ids)
            requester_ids = sorted({int(t.get("requester_id") or 0) for t in tickets.values()})
            users = zendesk.show_many_users(requester_ids)

            for ticket_id in batch_ids:
                if max_per_run and classified_this_run >= max_per_run:
                    break
                ticket = tickets.get(ticket_id)
                if ticket is None:
                    # Deleted/never-existed IDs are simply not served.
                    stats["skipped_missing"] = int(stats.get("skipped_missing") or 0) + 1
                    last_processed_id = ticket_id
                    continue
                requester_email = str(users.get(ticket.get("requester_id"), {}).get("email") or "")
                comments, comments_truncated = zendesk.fetch_ticket_comments(ticket_id)
                block, attachment_names, _desc_truncated = _format_ticket_block(ticket, comments, requester_email, desc_cap)
                # Partial evidence — description cap hit, or comments pages
                # beyond the first (this scanner reads one page; full
                # traversal is issue #41) — must never yield an automatic
                # verdict: skip the LLM call entirely and force `unsure`.
                partial_evidence = _desc_truncated or comments_truncated

                verdict = "unsure"
                if partial_evidence:
                    reason = (
                        "description truncated beyond LLM_SCAN_DESC_CHAR_CAP"
                        if _desc_truncated
                        else "comments paginated beyond first page (issue #41)"
                    )
                    if _desc_truncated and comments_truncated:
                        reason = "description truncated and comments paginated (issue #41)"
                    stats["forced_unsure_partial_evidence"] = (
                        int(stats.get("forced_unsure_partial_evidence") or 0) + 1
                    )
                else:
                    payload = prompt_text + "\n\n---\n\n" + _FORMAT_INSTRUCTIONS + "\n\nTICKET DATA:\n" + block
                    verdict = "unsure"
                    reason = "no response obtained"
                    # Transport-level request failures (LLMRequestError)
                    # propagate: aborting the run without consuming the range.
                    # A 2xx response with an unusable body
                    # (MalformedCompletionError) is model misbehavior, not
                    # transport: this ticket falls back to `unsure` for human
                    # review and the scan continues to the next ticket.
                    try:
                        raw_response = llm.classify(payload)
                        parsed_verdict, parsed_reason = _parse_verdict(raw_response, ticket_id)
                        verdict, reason = parsed_verdict, parsed_reason
                    except MalformedCompletionError as exc:
                        verdict, reason = "unsure", f"malformed completion: {exc}"

                stats[verdict] = int(stats.get(verdict) or 0) + 1
                if verdict == "deny":
                    _append_dedup(denylist_path, ticket_id, deny_ids)
                elif verdict == "unsure":
                    _append_review(review_path, ticket_id, reason, reviewed)
                classified_this_run += 1
                last_processed_id = ticket_id
                if classified_this_run % 100 == 0:
                    print(f"  … {classified_this_run} classified this run (at ID {ticket_id})")

            # Cursor from the last ID actually processed. When the cap hit
            # mid-batch (break above), last_processed_id stays just below the
            # unvisited tail, so the next run re-serves those IDs. When the
            # whole batch was consumed, this equals batch_ids[-1].
            if last_processed_id is None:
                # Whole batch skipped by the outer-loop condition (cap already
                # met before the batch started): rewind to the batch start.
                next_id = batch_ids[0]
            else:
                next_id = last_processed_id + 1
            _save_state(
                state_path,
                {"next_id": next_id, "stop_id": stop_id, "prompt_sha256": prompt_sha, "stats": stats},
            )
    except KeyboardInterrupt:
        print("\nInterrupted; state saved through the last completed batch.")
        _save_state(state_path, {"next_id": next_id, "stop_id": stop_id, "prompt_sha256": prompt_sha, "stats": stats})
        _summary(stats, classified_this_run, 0, stop_id, next_id)
        sys.exit(130)
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        # Infra failure: abort without advancing past unclassified tickets.
        print(f"error: aborted after infra failure: {exc}", file=sys.stderr)
        print("        state saved through the last completed batch; re-run to resume.")
        _save_state(state_path, {"next_id": next_id, "stop_id": stop_id, "prompt_sha256": prompt_sha, "stats": stats})
        _summary(stats, classified_this_run, 0, stop_id, next_id)
        sys.exit(1)

    print(f"Pass segment complete at next_id={next_id} (stop_id={stop_id}).")
    if next_id > stop_id:
        print("Full pass complete.")
    _save_state(state_path, {"next_id": next_id, "stop_id": stop_id, "prompt_sha256": prompt_sha, "stats": stats})
    _summary(stats, classified_this_run, 0, stop_id, next_id)


def _write_completion(state_path: Path, next_id: int, stop_id: int, prompt_sha: str, stats: dict[str, Any]) -> None:
    _save_state(state_path, {"next_id": next_id, "stop_id": stop_id, "prompt_sha256": prompt_sha, "stats": stats})


def _summary(stats: dict[str, Any], classified_this_run: int, failures: int, stop_id: int, next_id: int) -> None:
    print(
        "\nSummary:\n"
        f"  verdict totals (all runs so far): "
        f"deny={stats.get('deny', 0)} unsure={stats.get('unsure', 0)} "
        f"allow={stats.get('allow', 0)} missing={stats.get('skipped_missing', 0)} "
        f"forced-unsure-partial={stats.get('forced_unsure_partial_evidence', 0)}\n"
        f"  this run: classified={classified_this_run} llm_failures={failures}\n"
        f"  cursor: next_id={next_id} of stop_id={stop_id}"
    )


if __name__ == "__main__":
    main()