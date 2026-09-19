# Zendesk ticket denylist workflow

Purpose: keep tickets that must never appear in the knowledge base out of
it — including legacy **archived** tickets that pre-date the
`ach_request`/`sensitive` tags and can no longer be tagged in Zendesk
(tracking issue: #38).

The mechanism has four parts:

1. **Connector denylist** (`ZENDESKTICKET_DENYLIST_FILES`) — the connector
   reads denylist files at startup and excludes those ticket IDs from the
   manifest, both for newly crawled tickets and for previously synced
   (carried-forward) ones. The next normal sync run purges denylisted
   tickets' KB files (`tickets/<id>.md` and `attachments/<id>/…`) via the
   standard deleted-diff. No standalone mutation is needed, so
   `manifest_state.json` stays consistent with the KB.
2. **LLM scanner** (`scripts/zendesk_llm_denylist_scan.py`) — one-off
   helper that walks ticket IDs `1..STOP_ID` in creation order and asks an
   OpenAI-compatible LLM whether each ticket matches the deny criteria in
   your prompt file, appending `deny` verdicts to the denylist and
   `unsure` verdicts to a review file.
3. **Prompt file** (`scripts/prompts/zendesk_legacy_deny.prompt.md`) —
   editable description of *what* to deny; the scanner appends its own
   strict-JSON response-format instructions.
4. **Verifier** (`scripts/verify_zendesk_denylist.py`) — read-only check
   that every denylisted ID is absent from the KB.

## Denylist file format

One numeric ticket ID per line. Blank lines and lines starting with `#` are
ignored. Multiple files (comma-separated in `ZENDESKTICKET_DENYLIST_FILES`)
are unioned and deduplicated. `~` is expanded.

```text
# legacy credit-application tickets
45748
46023
```

```bash
export ZENDESKTICKET_DENYLIST_FILES=/path/to/legacy_deny.txt,/path/to/manual_deny.txt
```

Fail-closed: a missing/unreadable file or a non-numeric line raises
`ValueError` and the sync run aborts. A typo'd path must stop the run, not
warn and sync a sensitive ticket.

## Running the scanner

The scanner classifies `1..LLM_SCAN_STOP_TICKET_ID` — set the stop ID to the
first Zendesk ticket ID covered by the auto-tagging rules; tickets at or
above it are handled by the normal tag filters. IDs are walked in creation
order (verified: `tickets/show_many.json` serves archived tickets, so no
ticket in the range is unreachable) rather than by date: a date cutoff on
the incremental stream would mis-bound old tickets that received late
comments.

```bash
export ZENDESKTICKET_SUBDOMAIN=porky
export ZENDESKTICKET_USER=you@porky.com   # email; /token appended
export ZENDESKTICKET_TOKEN=...
export LLM_SCAN_STOP_TICKET_ID=70000      # boundary ID: first auto-tagged ticket
export LLM_SCAN_PROMPT_FILE=scripts/prompts/zendesk_legacy_deny.prompt.md
export LLM_SCAN_DENYLIST_FILE=/path/to/legacy_deny.txt
export LLM_SCAN_REVIEW_FILE=/path/to/review.txt
export LLM_SCAN_STATE_FILE=/path/to/scan_state.json
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://api.openai.com/v1   # any OpenAI-compatible backend
export OPENAI_MODEL=gpt-4o-mini

python3 scripts/zendesk_llm_denylist_scan.py
```

Optional: `LLM_SCAN_MAX_PER_RUN` (default `1000`, `0` = unlimited),
`LLM_SCAN_TIMEOUT_SECONDS` (default `120`), `LLM_SCAN_MAX_LLM_RETRIES`
(default `3`), `LLM_SCAN_DESC_CHAR_CAP` (default `6000`; oversized
descriptions are truncated and forced to `unsure`).

Behavior:

- **Resumable**: state is the next ticket ID; re-run after any abort and it
  continues. Changing the prompt file or stop ID between runs aborts (use
  `--reset` to restart the pass intentionally).
- **Fail-closed**: LLM outages abort the run rather than guessing;
  malformed responses fall back to `unsure` for human review; verdicts are
  categorical `deny|unsure|allow` (no confidence scores).
- **Review file**: lines like `45748  # unsure: non-JSON response  (…)`.
  Triage each by adding the ID to a denylist file (deny it) or doing
  nothing (accept as allowed). The scanner deduplicates against existing
  files, so reviewed IDs are never re-appended on later runs.

## Purging and verifying

1. Run the scanner (possibly across multiple runs) until the pass completes.
2. Add any human-review `deny` decisions from the review file.
3. Run a normal `oikb sync zendesktickets:<subdomain>` — denylisted tickets
   drop out of the manifest and their KB files are deleted by the sync's
   cleanup step.
4. Verify:

```bash
export OPEN_WEBUI_URL=...
export OPEN_WEBUI_API_KEY=...
python3 scripts/verify_zendesk_denylist.py <kb-id> /path/to/legacy_deny.txt
```

Exit codes: `0` clean, `1` leaked (denylisted ticket still has KB files —
re-run the sync; do not hand-delete), `2` error (bad denylist or unreachable
KB).

## Limitations and operational notes

- **Re-admitting a false positive**: removing an ID from the denylist stops
  future exclusion but does not re-import the ticket, because its entries
  were also dropped from `manifest_state.json`. To re-import, use
  `scripts/reset_zendesktickets_checkpoint.py` (full re-crawl re-applies
  filters) or edit the ticket in Zendesk so `updated_at` advances and the
  incremental crawl picks it up again.
- The scanner and verifier are standalone stdlib-only scripts; they do not
  import oikb and can run anywhere Python 3.9+ is available.
- The scanner writes only text files; it never contacts the KB or mutates
  any oikb state.