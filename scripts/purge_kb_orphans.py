#!/usr/bin/env python3
"""Purge orphaned File rows from an Open WebUI knowledge base.

Background: oikb uploads files via ``POST /api/v1/files/`` and links them to
the KB in a second step. When background processing fails (embedding 429s,
duplicate-content errors), the File row and its stored blob remain but are
never linked. ``GET /knowledge/{id}/sync/diff`` indexes only *linked* files,
so those orphans are invisible and the sync re-uploads the same content on
every run -- each failure adding another orphan.

This script finds File rows whose ``meta.data.knowledge_id`` matches the KB
but that are not present in the KB's ``knowledge_file`` link table, then for
each orphan (files NOT linked to any other KB):

1. deletes its vectors from the KB collection (by ``file_id``) and from the
   per-file ``file-{id}`` collection,
2. deletes the stored blob (row deletion is skipped if the blob delete
   raises, so the orphan stays discoverable for a later re-run),
3. deletes the File row -- unless the file is linked to another knowledge
   base (then it keeps its File row and per-file collection, and only the
   vectors it may have partially indexed into THIS KB collection, filtered
   by ``file_id``, are scrubbed).

Default mode is DRY-RUN: it prints what it would do and changes nothing.
Pass ``--apply`` to actually purge. ``--verbose`` prints every candidate.

Usage (script is not mounted into the container, so copy or pipe it):

    docker cp scripts/purge_kb_orphans.py open-webui:/tmp/
    docker exec open-webui python /tmp/purge_kb_orphans.py <kb_id>
    docker exec open-webui python /tmp/purge_kb_orphans.py <kb_id> --apply

or without touching the container filesystem (``python -`` passes trailing
argv through):

    docker exec -i open-webui python - <kb_id> --apply \\
        < scripts/purge_kb_orphans.py

IMPORTANT: stop the oikb sync daemon first (``docker stop oikb`` or
equivalent via your container manager), otherwise in-flight uploads in
pending/processing state would be misidentified as orphans and killed.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Open WebUI runs from /app/backend; a bare `python /tmp/...` shell does not
# have it on sys.path, so add it when present.
for _backend in ("/app/backend",):
    if Path(_backend).is_dir() and _backend not in sys.path:
        sys.path.insert(0, _backend)


async def purge(kb_id: str, apply: bool, verbose: bool) -> int:
    # Imports are deliberately late so ``--help`` works outside the container.
    from sqlalchemy import delete as sa_delete, func, select

    from open_webui.internal.db import get_async_db_context
    from open_webui.models.files import File
    from open_webui.models.knowledge import Knowledge, KnowledgeFile
    from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
    from open_webui.storage.provider import Storage

    removed_files = 0
    scrubbed_only = 0
    failed = 0

    async with get_async_db_context() as db:
        from sqlalchemy import exists as sa_exists

        result = await db.execute(
            select(Knowledge).filter(Knowledge.id == kb_id)
        )
        kb = result.scalars().one_or_none()
        if kb is None:
            print(f"Knowledge base {kb_id} not found.")
            return 2
        print(f"Knowledge base: {kb.name} ({kb.id})")

        linked_count = await db.scalar(
            select(func.count())
            .select_from(KnowledgeFile)
            .filter(KnowledgeFile.knowledge_id == kb_id)
        )
        print(f"Linked files: {linked_count}")

        # Hashes of linked files must not be purged from the KB collection,
        # or healthy duplicate copies would lose their embeddings.
        # (Join through knowledge_file -- avoids a large IN(...) bind list.)
        protected_hashes = {
            h for h in (await db.execute(
                select(File.hash)
                .join(KnowledgeFile, KnowledgeFile.file_id == File.id)
                .where(KnowledgeFile.knowledge_id == kb_id)
            )).scalars().all() if h
        }
        print(f"Protected hashes: {len(protected_hashes)}")

        orphans = (await db.execute(
            select(File)
            .where(
                File.meta["data"]["knowledge_id"].as_string() == kb_id,
                ~sa_exists(select(KnowledgeFile).where(
                    KnowledgeFile.knowledge_id == kb_id,
                    KnowledgeFile.file_id == File.id,
                )),
            )
            .order_by(File.created_at)
        )).scalars().all()
        print(
            f"Orphaned uploads aimed at this KB: {len(orphans)}"
            + ("" if apply else "  (DRY RUN -- no changes will be made)")
            + "\n"
        )

        # Prefetch, once: which orphan ids are also linked to other KBs.
        # (A file that failed linking here may have been linked elsewhere
        # later; those keep their File row and get a vector scrub only.)
        # NOTE: cannot use .in_(orphan_ids) here -- 270k+ bind params would
        # exceed PostgreSQL's 65535-parameter limit. Grab every file_id that
        # is linked to some *other* KB (no bind params) and intersect locally.
        orphan_id_set = {f.id for f in orphans}
        other_ids = (
            set(
                (await db.execute(
                    select(KnowledgeFile.file_id).where(
                        KnowledgeFile.knowledge_id != kb_id
                    )
                )).scalars().all()
            )
            & orphan_id_set
        )
        print(f"Linked to other KB(s): {len(other_ids)}\n")

        # Orphan hashes that no linked file uses -- each needs exactly ONE
        # hash-delete across the whole KB collection, not one per orphan row.
        # Computed AFTER the prefetch; protected_hashes is refreshed below,
        # right before the scrub, so a file linked to this KB mid-run that
        # shares a hash with an older upload is not scrubbed.
        unprotected_orphan_hashes = {
            f.hash for f in orphans if f.hash and f.hash not in protected_hashes
        }
        print(f"Distinct orphan hashes to scrub: {len(unprotected_orphan_hashes)}")

        # Phase timers so the progress lines show where time goes.
        import time
        started = time.monotonic()
        t_vec = t_db = 0.0

        # One-time hash scrub (must not race the concurrent file deletes).
        # A failed hash-delete is recorded in failed_hashes: vectors keyed
        # only by that hash become unrecoverable once the File rows are
        # gone, so orphans carrying a failed hash keep their File rows and
        # are only counted as errors for a later re-run to finish.
        failed_hashes: set[str] = set()
        if apply:
            # Refresh: links may have appeared since protected_hashes was
            # computed (rows can be linked to this KB mid-run).
            protected_hashes = {
                h for h in (await db.execute(
                    select(File.hash)
                    .join(KnowledgeFile, KnowledgeFile.file_id == File.id)
                    .where(KnowledgeFile.knowledge_id == kb_id)
                )).scalars().all() if h
            }
            for h in list(unprotected_orphan_hashes):
                if h in protected_hashes:
                    unprotected_orphan_hashes.discard(h)
                    continue
                try:
                    await ASYNC_VECTOR_DB_CLIENT.delete(
                        collection_name=kb_id, filter={"hash": h}
                    )
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    failed_hashes.add(h)
                    print(f"  ERROR scrubbing hash {h}: {e}", file=sys.stderr)
                unprotected_orphan_hashes.discard(h)

        VSCRUB_CONCURRENCY = 16
        vscrub_sem = asyncio.Semaphore(VSCRUB_CONCURRENCY)

        async def vscrub_kb(f):
            """Scrub only this KB's collection (file_id filter). For orphans
            that are linked to another KB: keep their per-file collection and
            File row intact."""
            try:
                async with vscrub_sem:
                    await ASYNC_VECTOR_DB_CLIENT.delete(
                        collection_name=kb_id, filter={"file_id": f.id}
                    )
                return None
            except Exception as e:  # noqa: BLE001
                return e

        async def vscrub_all(f):
            """Full scrub: this KB's collection plus the per-file one."""
            try:
                async with vscrub_sem:
                    await ASYNC_VECTOR_DB_CLIENT.delete(
                        collection_name=kb_id, filter={"file_id": f.id}
                    )
                    await ASYNC_VECTOR_DB_CLIENT.delete(
                        collection_name=f"file-{f.id}"
                    )
                return None
            except Exception as e:  # noqa: BLE001
                return e

        done = 0
        total = len(orphans)
        PROGRESS_EVERY = 5000
        next_progress = min(200, total) if total else 0
        BATCH = 200

        for i in range(0, total, BATCH):
            batch = orphans[i : i + BATCH]
            # Re-check other-KB linkage per batch: the prefetch above is a
            # point-in-time snapshot, and a link can appear mid-run (e.g.
            # a sync daemon restarting). One EXISTS-free query per batch
            # (BATCH bind params, well under PostgreSQL's 65535 limit).
            # NOTE: linkage to ANY KB (not just other KBs) protects the
            # row -- a re-link to this same KB mid-run must also survive,
            # or the script would delete the blob of a live file.
            batch_ids = [f.id for f in batch]
            still_linked = set(
                (await db.execute(
                    select(KnowledgeFile.file_id).where(
                        KnowledgeFile.file_id.in_(batch_ids),
                    )
                )).scalars().all()
            )
            # Two survival cases are kept apart so each is handled correctly:
            #   - still linked (to any KB): vector-scrub only, row survives
            #   - hash-scrub failure: fully retained (skipped below) so a
            #     re-run can retry; the error was already counted above.
            doom_pending = [
                f for f in batch
                if f.id not in still_linked
                and f.hash not in failed_hashes
            ]
            scrub_only = [
                f for f in batch
                if f.id in still_linked and f.hash not in failed_hashes
            ]

            if apply:
                # Vector scrub: 16 concurrent Qdrant round-trip pairs.
                # scrub_only (linked elsewhere) gets the KB-collection-only
                # variant so other KBs' retrieval keeps working.
                # hash_retained is skipped entirely: its rows survive on
                # purpose, and the scrub error was already tallied.
                t0 = time.monotonic()
                doomed_results = await asyncio.gather(
                    *(vscrub_all(f) for f in doom_pending)
                )
                scrub_results = await asyncio.gather(
                    *(vscrub_kb(f) for f in scrub_only)
                )
                t_vec += time.monotonic() - t0
                vec_errors = [
                    (f.id, e)
                    for f, e in zip(doom_pending, doomed_results)
                    if e
                ] + [
                    (f.id, e)
                    for f, e in zip(scrub_only, scrub_results)
                    if e
                ]
                for fid, e in vec_errors:
                    failed += 1
                    print(f"  ERROR on {fid}: {e}", file=sys.stderr)

                # Vectors scrubbed; anything that failed keeps its error
                # count and is treated as NOT purged below.
                vec_failed_ids = {fid for fid, _ in vec_errors}

                # Blob delete FIRST: if it raises, the File row is retained
                # so a later re-run can still find and finish the cleanup.
                t0 = time.monotonic()
                blob_ok: list[File] = []
                for f in doom_pending:
                    if f.id in vec_failed_ids:
                        continue
                    if not f.path:
                        # No blob backing the row (e.g. its file was already
                        # removed from storage): nothing to delete there, so
                        # the row itself can be removed.
                        blob_ok.append(f)
                        continue
                    try:
                        await asyncio.to_thread(Storage.delete_file, f.path)
                        blob_ok.append(f)
                    except Exception as e:  # noqa: BLE001
                        failed += 1
                        print(
                            f"  ERROR deleting blob {f.id}: {e}",
                            file=sys.stderr,
                        )
                # DB delete: one statement per batch (BATCH ids << 65535),
                # only for rows whose blob (and vectors) are already gone.
                if blob_ok:
                    await db.execute(
                        sa_delete(File).where(
                            File.id.in_([f.id for f in blob_ok])
                        )
                    )
                t_db += time.monotonic() - t0
            else:
                for f in batch:
                    if not verbose:
                        continue
                    if f.id in other_ids:
                        print(
                            f"  would scrub vectors only {f.id} ({f.filename})"
                            " -- linked elsewhere"
                        )
                    else:
                        print(
                            f"  would purge File row {f.id} ({f.filename})"
                        )

            removed_files += len(blob_ok) if apply else len(doom_pending)
            scrubbed_only += len(scrub_only)
            done += len(batch)

            if done >= next_progress or done >= total:
                if apply:
                    # Incremental commit: bounds WAL/transaction size and
                    # makes progress visible to outside count queries.
                    await db.commit()
                elapsed = time.monotonic() - started
                rate = done / elapsed
                eta_min = (total - done) / rate / 60 if rate > 0 else float("inf")
                print(
                    f"  ... {done}/{total} processed "
                    f"({rate:.0f}/s, ~{eta_min:.0f} min left); "
                    f"purged so far: {removed_files}, errors: {failed}"
                    + (
                        f" [vec {t_vec:.0f}s, db+blob {t_db:.0f}s]"
                        if apply
                        else ""
                    ),
                    flush=True,
                )
                next_progress = done + PROGRESS_EVERY

        if apply:
            await db.commit()

    mode = "Purged" if apply else "Would purge"
    print(
        f"\n{mode} {removed_files} orphaned file(s); "
        f"{scrubbed_only} vector-scrub only (linked elsewhere); "
        f"{failed} error(s)."
        + (f"  [vector {t_vec:.0f}s, db+blob {t_db:.0f}s]" if apply else "")
    )
    if not apply:
        print("Re-run with --apply to make these changes.")
    return 0 if failed == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Purge orphaned (unlinked) File rows from a knowledge base."
    )
    parser.add_argument("kb_id", help="Knowledge base id")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this flag the script is a dry run.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print every candidate file."
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(purge(args.kb_id, args.apply, args.verbose)))


if __name__ == "__main__":
    main()
