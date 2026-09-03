#!/usr/bin/env python3
"""Purge orphaned File rows from an Open WebUI knowledge base.

Background: oikb uploads files via ``POST /api/v1/files/`` and links them to
the KB in a second step. When background processing fails (embedding 429s,
duplicate-content errors), the File row and its stored blob remain but are
never linked. ``GET /knowledge/{id}/sync/diff`` indexes only *linked* files,
so those orphans are invisible and the sync re-uploads the same content on
every run -- each failure adding another orphan.

This script finds File rows whose ``meta.data.knowledge_id`` matches the KB
but that are not present in the KB's ``knowledge_file`` link table, then:

1. deletes their vectors from the KB collection (by ``file_id``, and by
   ``hash`` only when that hash is NOT also used by a linked file),
2. deletes vectors from the per-file ``file-{id}`` collection,
3. deletes the File row and its stored blob -- unless the file is linked to
   another knowledge base (then only the vector scrub happens).

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
        result = await db.execute(
            select(Knowledge).filter(Knowledge.id == kb_id)
        )
        kb = result.scalars().one_or_none()
        if kb is None:
            print(f"Knowledge base {kb_id} not found.")
            return 2
        print(f"Knowledge base: {kb.name} ({kb.id})")

        linked_ids = set(
            (await db.execute(
                select(KnowledgeFile.file_id).filter(
                    KnowledgeFile.knowledge_id == kb_id
                )
            )).scalars().all()
        )
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
                File.id.notin_(linked_ids),
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
        unprotected_orphan_hashes = {
            f.hash for f in orphans if f.hash and f.hash not in protected_hashes
        }
        print(f"Distinct orphan hashes to scrub: {len(unprotected_orphan_hashes)}")

        # Phase timers so the progress lines show where time goes.
        import time
        started = time.monotonic()
        t_vec = t_db = 0.0

        # One-time hash scrub (must not race the concurrent file deletes).
        if apply:
            for h in list(unprotected_orphan_hashes):
                try:
                    await ASYNC_VECTOR_DB_CLIENT.delete(
                        collection_name=kb_id, filter={"hash": h}
                    )
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    print(f"  ERROR scrubbing hash {h}: {e}", file=sys.stderr)
                unprotected_orphan_hashes.discard(h)

        VSCRUB_CONCURRENCY = 16
        vscrub_sem = asyncio.Semaphore(VSCRUB_CONCURRENCY)

        async def vscrub(f):
            """Scrub one orphan's vectors; return None on success."""
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
            doomed = [f for f in batch if f.id not in other_ids]
            scrub_batch = [f for f in batch if f.id in other_ids]

            if apply:
                # Vector scrub: 16 concurrent Qdrant round-trip pairs.
                t0 = time.monotonic()
                results = await asyncio.gather(*(vscrub(f) for f in batch))
                t_vec += time.monotonic() - t0
                for f, e in zip(batch, results):
                    if e:
                        failed += 1
                        print(f"  ERROR on {f.id}: {e}", file=sys.stderr)

                # DB delete: one statement per batch (BATCH ids << 65535).
                t0 = time.monotonic()
                if doomed:
                    await db.execute(
                        sa_delete(File).where(
                            File.id.in_([f.id for f in doomed])
                        )
                    )
                    for f in doomed:
                        if not f.path:
                            continue
                        try:
                            await asyncio.to_thread(Storage.delete_file, f.path)
                        except Exception as e:  # noqa: BLE001
                            failed += 1
                            print(
                                f"  ERROR deleting blob {f.id}: {e}",
                                file=sys.stderr,
                            )
                t_db += time.monotonic() - t0
            else:
                for f in batch:
                    if verbose:
                        print(
                            f"  would purge File row {f.id} ({f.filename})"
                        )

            removed_files += len(doomed)
            scrubbed_only += len(scrub_batch)
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
