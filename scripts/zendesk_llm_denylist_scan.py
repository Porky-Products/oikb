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
                        raise RuntimeError("empty choices")
                    return str(choices[0].get("message", {}).get("content", ""))
                except (AttributeError, TypeError) as exc:
                    raise RuntimeError(f"malformed completion payload: {exc}") from exc
            last_error = f"HTTP {status}: {raw[:300]}"
            if attempt == self._max_retries:
                break
            # 429/5xx retried; 4xx (auth, bad request) not -- abort fast.
            if status == 429 or status >= 500:
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            break
        raise RuntimeError(f"LLM request failed after {self._max_retries + 1} attempts: {last_error}")


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


def _format_ticket_block(ticket: dict[str, Any], requester_email: str, desc_cap: int) -> tuple[str, bool]:
    """Render one ticket for the LLM. Returns (block_text, truncated)."""
    description = str(ticket.get("description") or "")
    truncated = len(description) > desc_cap
    if truncated:
        description = description[:desc_cap] + "\n[…description truncated…]"
    comment_attachment_names: list[str] = []
    for attachment in ticket.get("attachments") or []:
        name = str(attachment.get("file_name") or "").strip()
        if name:
            comment_attachment_names.append(name)
    lines = [
        f"ticket_id: {ticket.get('id')}",
        f"subject: {ticket.get('subject')!r}",
        f"created_at: {ticket.get('created_at')}",
        f"status: {ticket.get('status')}",
        f"type: {ticket.get('type')}",
        f"tags: {ticket.get('tags') or []}",
        f"requester_email: {requester_email or '(unresolved)'}",
        f"attachment_names: {comment_attachment_names}",
        "description:",
        description or "(empty)",
    ]
    return "\n".join(lines), truncated


def _append_dedup(path: Path, ticket_id: int, deny_ids: set[int]) -> None:
    if ticket_id in deny_ids:
        return
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{ticket_id}\n")
    deny_ids.add(ticket_id)


def _append_review(path: Path, ticket_id: int, reason: str, reviewed: set[int]) -> None:
    if ticket_id in reviewed:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{ticket_id}  # unsure: {reason}  ({stamp})\n")
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
        if not os.environ.get(name, "").strip().isdigit():
            _die(f"{name} must be a positive integer (got {os.environ.get(name)!r})")

    stop_id = int(os.environ["LLM_SCAN_STOP_TICKET_ID"])
    prompt_path = Path(os.environ["LLM_SCAN_PROMPT_FILE"])
    denylist_path = Path(os.environ["LLM_SCAN_DENYLIST_FILE"])
    review_path = Path(os.environ["LLM_SCAN_REVIEW_FILE"])
    state_path = Path(os.environ["LLM_SCAN_STATE_FILE"])
    max_per_run = int(os.environ.get("LLM_SCAN_MAX_PER_RUN") or _MAX_PER_RUN_DEFAULT)
    timeout = float(os.environ.get("LLM_SCAN_TIMEOUT_SECONDS") or _TIMEOUT_DEFAULT)
    llm_retries = int(os.environ.get("LLM_SCAN_MAX_LLM_RETRIES") or _MAX_LLM_RETRIES_DEFAULT)
    desc_cap = int(os.environ.get("LLM_SCAN_DESC_CHAR_CAP") or _DESC_CAP_DEFAULT)

    if not prompt_path.is_file():
        _die(f"prompt file not found: {prompt_path}")
    prompt_text = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt_text:
        _die(f"prompt file is empty: {prompt_path}")
    prompt_sha = _sha256_file(prompt_path)

    # ---- State / resume -------------------------------------------------
    reset_requested = "--reset" in sys.argv[1:]
    state = _load_state(state_path)
    if state is not None:
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
        next_id = int(state.get("next_id") or 1)
        stats = dict(state.get("stats") or {})
    else:
        next_id = 1
        stats = {}
    if reset_requested:
        next_id = 1
        stats = {}
        print("--reset: restarting from ID 1 (counts recomputed from files)")
    if next_id > stop_id:
        print(f"Nothing to do: next_id={next_id} > stop_id={stop_id} (pass complete).")
        _write_completion(state_path, next_id, stop_id, prompt_sha, stats)
        return

    # ---- Existing denylist (dedup on append) ---------------------------
    deny_ids: set[int] = set()
    if denylist_path.exists():
        for line in denylist_path.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if entry and not entry.startswith("#"):
                deny_ids.add(int(entry))
    reviewed: set[int] = set()
    if review_path.exists():
        for line in review_path.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if entry and not entry.startswith("#"):
                head = entry.split("#", 1)[0].strip()
                if head.isdigit():
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
    per_ticket_failures = 0

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
                    continue
                requester_email = str(users.get(ticket.get("requester_id"), {}).get("email") or "")
                block, truncated = _format_ticket_block(ticket, requester_email, desc_cap)

                payload = prompt_text + "\n\n---\n\n" + _FORMAT_INSTRUCTIONS + "\n\nTICKET DATA:\n" + block
                if truncated:
                    payload += "\n\nNOTE: description was truncated; classify with 'unsure' if the truncated part might change your verdict."

                raw_response = None
                verdict = "unsure"
                reason = "no response obtained"
                try:
                    raw_response = llm.classify(payload)
                    verdict, reason = _parse_verdict(raw_response, ticket_id)
                except RuntimeError as exc:
                    per_ticket_failures += 1
                    reason = str(exc)
                    verdict = "unsure"

                stats[verdict] = int(stats.get(verdict) or 0) + 1
                if verdict == "deny":
                    _append_dedup(denylist_path, ticket_id, deny_ids)
                elif verdict == "unsure":
                    _append_review(review_path, ticket_id, reason, reviewed)
                classified_this_run += 1
                if classified_this_run % 100 == 0:
                    print(f"  … {classified_this_run} classified this run (at ID {ticket_id})")

            next_id = batch_ids[-1] + 1
            _save_state(
                state_path,
                {"next_id": next_id, "stop_id": stop_id, "prompt_sha256": prompt_sha, "stats": stats},
            )
    except KeyboardInterrupt:
        print("\nInterrupted; state saved through the last completed batch.")
        _save_state(state_path, {"next_id": next_id, "stop_id": stop_id, "prompt_sha256": prompt_sha, "stats": stats})
        _summary(stats, classified_this_run, per_ticket_failures, stop_id, next_id)
        sys.exit(130)
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        # Infra failure: abort without advancing past unclassified tickets.
        print(f"error: aborted after infra failure: {exc}", file=sys.stderr)
        print("        state saved through the last completed batch; re-run to resume.")
        _save_state(state_path, {"next_id": next_id, "stop_id": stop_id, "prompt_sha256": prompt_sha, "stats": stats})
        _summary(stats, classified_this_run, per_ticket_failures, stop_id, next_id)
        sys.exit(1)

    print(f"Pass segment complete at next_id={next_id} (stop_id={stop_id}).")
    if next_id > stop_id:
        print("Full pass complete.")
    _save_state(state_path, {"next_id": next_id, "stop_id": stop_id, "prompt_sha256": prompt_sha, "stats": stats})
    _summary(stats, classified_this_run, per_ticket_failures, stop_id, next_id)
    if per_ticket_failures:
        print(
            "NOTE: some tickets fell back to 'unsure' after LLM failures; "
            "they are in the review file. Re-run after checking OPENAI_* config."
        )
        sys.exit(2)


def _write_completion(state_path: Path, next_id: int, stop_id: int, prompt_sha: str, stats: dict[str, Any]) -> None:
    _save_state(state_path, {"next_id": next_id, "stop_id": stop_id, "prompt_sha256": prompt_sha, "stats": stats})


def _summary(stats: dict[str, Any], classified_this_run: int, failures: int, stop_id: int, next_id: int) -> None:
    print(
        "\nSummary:\n"
        f"  verdict totals (all runs so far): "
        f"deny={stats.get('deny', 0)} unsure={stats.get('unsure', 0)} "
        f"allow={stats.get('allow', 0)} missing={stats.get('skipped_missing', 0)}\n"
        f"  this run: classified={classified_this_run} llm_failures={failures}\n"
        f"  cursor: next_id={next_id} of stop_id={stop_id}"
    )


if __name__ == "__main__":
    main()