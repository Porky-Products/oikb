#!/usr/bin/env python3
"""Reset the ZendeskTicketsConnector checkpoint to force a full re-crawl.

Background
----------
The connector keeps two durable checkpoint records so an incremental
crawl can resume where the last one ended:

  1. ``<state_dir>/resume_checkpoint.txt`` -- an ISO timestamp, or the
     ``STALLED_EQUAL_TIMESTAMP:<ts>`` sentinel written after an
     equal-boundary stall. ``_load_checkpoint`` prefers this file.
  2. ``<state_dir>/manifest_state.json``'s ``checkpoint`` key -- the
     fallback when the file is missing.

Ticket tag and status filtering (``_should_include_ticket``) only runs
for tickets Zendesk actually re-serves during a run. The persisted
``ticket_files`` map -- "everything we already synced" -- is carried
forward every run and is never re-filtered. A ticket synced *before* a
tag was added to ``ZENDESKTICKET_EXCLUDETAG`` therefore stays in the
knowledge base indefinitely unless it is later edited on the Zendesk
side (an edit bumps ``updated_at`` so the incremental crawl re-fetches
and re-filters it).

What this script does
---------------------
1. Clears BOTH checkpoint records -- the file is archived and the
   ``checkpoint`` key is removed from ``manifest_state.json`` -- so the
   next run loads ``datetime.min`` and formats ``start_time=0``: a
   full incremental re-crawl of Zendesk history from the beginning.
2. PRESERVES ``ticket_files`` (and every other state key): the manifest
   stays complete, so there is no mass delete/re-add of the KB. As the
   re-crawl re-serves each ticket it is re-evaluated against the
   CURRENT tag/status filters; newly-excluded tickets drop out of the
   manifest and sync.py's normal diff removes their KB files. Purge of
   wrongly-synced tickets (e.g. tag ``sap:material_request``) therefore
   trickles in over the re-crawl rather than happening in one shot.
3. Backs up everything it mutates: ``manifest_state.json.bak-<ts>``
   before rewriting, and the checkpoint file is *moved* to
   ``resume_checkpoint.txt.bak-<ts>`` (an atomic archive-plus-remove),
   so a bad reset is one file restore away.

Ordering makes an interrupted ``--apply`` safe: the state rewrite
happens before the file move. If interrupted between them, the
checkpoint file still exists and ``_load_checkpoint``'s file-first
precedence keeps the old cursor -- the daemon will not prematurely
re-crawl, and re-running the script completes the reset. Re-running
after a completed reset reports "nothing to reset" (idempotent).

What to expect after ``--apply``
--------------------------------
* ``.run-cache`` (downloaded attachment bytes keyed by sha256) is
  purged automatically on the next daemon start (no durable checkpoint
  remains), so every kept ticket's attachments re-download during the
  re-crawl. Plan for the bandwidth/time.
* The per-run cap still applies: with e.g. 71,525 tracked tickets and
  ``ZENDESKTICKET_MAX_TICKETS_PER_RUN=1000`` the re-crawl needs ~72
  runs -- at a 24h sync interval that is over two months. Temporarily
  raise the cap or shorten the interval to converge sooner.

Usage
-----
State lives in the daemon container's filesystem (default
``/app/.oikb_state/zendesktickets/<subdomain>/`` because the oikb
image's working dir is /app). STOP THE OIKB SYNC DAEMON FIRST -- a
daemon mid-run can checkpoint while (or after) you mutate state and
undo the reset. Then, inside the container:

    docker cp scripts/reset_zendesktickets_checkpoint.py oikb:/tmp/
    docker exec -it oikb python /tmp/reset_zendesktickets_checkpoint.py            # dry run
    docker exec -it oikb python /tmp/reset_zendesktickets_checkpoint.py --apply

Or outside the container with an explicit directory:

    python scripts/reset_zendesktickets_checkpoint.py --state-dir <dir>

Stdlib only; no backend imports, so it runs anywhere Python 3 runs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_STATE_FILE = "manifest_state.json"
_CHECKPOINT_FILE = "resume_checkpoint.txt"
_DEFAULT_MAX_PER_RUN = 1000
_DEFAULT_INTERVAL_HOURS = 24.0


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _atomic_write_json(path: Path, payload: dict) -> None:
    # Mirror the connector's _save_state format (indent=2, sort_keys=True)
    # and its _atomic_write mechanics (tmp file + os.replace) so the file
    # on disk stays byte-compatible with what the daemon writes itself.
    tmp = path.parent / (path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def reset(state_dir: Path, apply: bool, verbose: bool) -> int:
    state_path = state_dir / _STATE_FILE
    checkpoint_path = state_dir / _CHECKPOINT_FILE

    print(f"State dir: {state_dir}")
    if not state_dir.is_dir():
        print(f"  error: state dir does not exist", file=sys.stderr)
        return 1

    # ---- Read current state before touching anything --------------------
    state: dict | None = None
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  error: cannot parse {_STATE_FILE}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(loaded, dict):
            print(
                f"  error: {_STATE_FILE} does not contain a JSON object",
                file=sys.stderr,
            )
            return 1
        state = loaded
        print(f"  {_STATE_FILE}: {state_path.stat().st_size:,} bytes")

    has_file_checkpoint = checkpoint_path.exists()
    has_state_checkpoint = bool(state and state.get("checkpoint"))

    print(f"  checkpoint file: ", end="")
    if has_file_checkpoint:
        raw = checkpoint_path.read_text(encoding="utf-8").strip()
        print(f"present ({raw})")
    else:
        print("absent")
    print(f"  state 'checkpoint' key: ", end="")
    if has_state_checkpoint:
        print(f"present ({state.get('checkpoint')})")
    else:
        print("absent")

    ticket_files = (state or {}).get("ticket_files") if state else None
    if isinstance(ticket_files, dict):
        print(f"  ticket_files entries: {len(ticket_files):,} (preserved)")
    elif state is not None:
        print(f"  ticket_files: absent (nothing to preserve)")

    if verbose and state is not None:
        print(f"  top-level state keys: {sorted(state.keys())}")

    if not has_file_checkpoint and not has_state_checkpoint:
        # Idempotent: a completed reset lands here on re-run.
        print("\nNothing to reset: no durable checkpoint found.")
        return 0

    # ---- Plan + expectations --------------------------------------------
    mode = "Would reset" if not apply else "Resetting"
    stamp = _stamp()
    print(f"\n{mode}:")
    if has_state_checkpoint:
        assert state is not None
        print(
            f"  - rewrite {_STATE_FILE} without 'checkpoint' "
            f"(backup: {_STATE_FILE}.bak-{stamp})"
        )
    else:
        print(f"  - {_STATE_FILE}: no 'checkpoint' key, left untouched")
    if has_file_checkpoint:
        print(
            f"  - archive {_CHECKPOINT_FILE} to "
            f"{_CHECKPOINT_FILE}.bak-{stamp} (removes it atomically)"
        )
    print()

    if isinstance(ticket_files, dict) and ticket_files:
        try:
            max_per_run = int(
                os.environ.get("ZENDESKTICKET_MAX_TICKETS_PER_RUN")
                or _DEFAULT_MAX_PER_RUN
            )
        except ValueError:
            max_per_run = _DEFAULT_MAX_PER_RUN
        max_per_run = max(1, max_per_run)
        tickets = len(ticket_files)
        runs = -(-tickets // max_per_run)  # ceil
        days = runs * _DEFAULT_INTERVAL_HOURS / 24.0
        print(
            f"NOTE: full re-crawl of {tickets:,} tracked tickets at "
            f"max {max_per_run}/run needs ~{runs} runs "
            f"(~{days:.0f} days at a {int(_DEFAULT_INTERVAL_HOURS)}h interval). "
            f"Temporarily raise ZENDESKTICKET_MAX_TICKETS_PER_RUN or shorten "
            f"the sync interval to converge sooner."
        )
        print(
            "NOTE: .run-cache auto-purges on next daemon start, so kept "
            "tickets' attachments re-download during the re-crawl."
        )
        print(
            "NOTE: KB files for newly-excluded tickets are removed as they "
            "are re-served and re-filtered (a trickle over the re-crawl), "
            "not in one shot."
        )

    if not apply:
        print("\nRe-run with --apply to make these changes.")
        return 0

    # ---- Apply (crash-safe order: state rewrite first, file move last) --
    if has_state_checkpoint:
        assert state is not None
        backup = state_path.parent / f"{_STATE_FILE}.bak-{stamp}"
        shutil.copy2(state_path, backup)
        print(f"  backed up {_STATE_FILE} -> {backup.name}")
        state.pop("checkpoint", None)
        _atomic_write_json(state_path, state)
        print(f"  rewrote {_STATE_FILE} without 'checkpoint'")
    if has_file_checkpoint:
        archive = checkpoint_path.parent / f"{_CHECKPOINT_FILE}.bak-{stamp}"
        os.replace(checkpoint_path, archive)
        print(f"  archived {_CHECKPOINT_FILE} -> {archive.name}")

    print(
        "\nDone. Start the oikb sync daemon; the next run re-crawls from "
        "the beginning and re-applies the current tag/status filters."
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Reset the ZendeskTickets connector checkpoint so the next run "
            "re-crawls and re-applies the current exclude-tag filters. "
            "Dry run by default; pass --apply to mutate state."
        )
    )
    default_subdomain = os.environ.get("ZENDESKTICKET_SUBDOMAIN") or "porky"
    parser.add_argument(
        "subdomain",
        nargs="?",
        default=default_subdomain,
        help=(
            "Zendesk subdomain (default: $ZENDESKTICKET_SUBDOMAIN or "
            f"{default_subdomain!r}); only used to derive the state dir."
        ),
    )
    parser.add_argument(
        "--state-dir",
        help=(
            "Explicit state directory (default: "
            "<cwd>/.oikb_state/zendesktickets/<subdomain>, matching the "
            "connector's own default, i.e. /app/... inside the container)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually reset. Without this flag the script is a dry run.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print extra state details."
    )
    args = parser.parse_args()

    state_dir = (
        Path(args.state_dir)
        if args.state_dir
        else Path.cwd() / ".oikb_state" / "zendesktickets" / args.subdomain
    )
    sys.exit(reset(state_dir, args.apply, args.verbose))


if __name__ == "__main__":
    main()
