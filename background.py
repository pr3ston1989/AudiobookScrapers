"""Finite maintenance batches while other service workers are still running."""

from pathlib import Path
from types import SimpleNamespace

from crawl_state import Coverage
from database import Database
from events import check_stop


COUNTERS = ("inserted", "updated", "skipped", "not_found", "incomplete", "maps",
            "listing_pages", "discovered", "new_candidates", "queued_new", "queued_repair",
            "queued_inherited_repair", "queued_id_gaps", "queued_refresh")


def other_workers(job):
    """Allow maintenance only after all initial services have received worker slots."""
    peers = job.get("peers")
    return bool(peers and (Path(peers) / "ready").exists() and any(path.stem != job["site"] for path in Path(peers).glob("*.running")))


def plan(site, settings, args, deadline, sweep):
    """Continue due queues, catalogs, LC ranges and one non-wrapping repair sweep."""
    if args.command != "update" or args.url or args.limit or getattr(args, "new_only", False):
        return None
    db = Database(site, settings["databases"][site])
    ledger = None
    try:
        ledger = Coverage(settings["coverage_database"], site, db.path)
        ledger.apply_aliases(db.known)
        repairs = []
        cursor = ledger.checkpoint("repair_cursor", 0)
        if not args.resume and settings["repair_limit"] > 0 and not sweep.get("repairs_done"):
            while True:
                check_stop()
                urls, end = db.repair_urls(cursor, min(25, settings["repair_limit"]), wrap=False)
                if not urls:
                    sweep["repairs_done"] = True
                    break
                cursor = end
                repairs = [url for url in urls if ledger.due(url, deadline)]
                if repairs:
                    break
            if not repairs:
                ledger.set_checkpoint("repair_cursor", cursor)
        queued = any(item["id"] not in sweep.get("attempted_tasks", set()) and ledger.due(item["url"], deadline) for item in db.pending()) or bool(ledger.work(db.known, deadline))
        catalog = not args.resume and not getattr(args, "fast", False) and (bool(ledger.pending_page(deadline)) or bool(ledger.checkpoint('sitemaps_pending', [])))
        gaps = (site == "lubimyczytac" and not args.resume and settings["lc_id_batch"] > 0
                and ledger.checkpoint("lc_id_cursor", 0) < ledger.checkpoint("lc_id_target", 0))
        if not (repairs or queued or catalog or gaps):
            return None
        batch = SimpleNamespace(**vars(args))
        batch.background = True
        batch.background_urls = repairs
        batch.background_repair_cursor = cursor
        batch.background_catalog = catalog
        batch.refresh_limit = 0
        batch.no_backup = True
        batch.limit = 25
        return batch
    finally:
        if ledger:
            ledger.close()
        db.close()


def merge(previous, current):
    """Preserve initial statistics and sum batches in one final service report."""
    result = {**previous, **current}
    result["before"] = previous.get("before", current.get("before"))
    for field in COUNTERS:
        if field in previous or field in current:
            result[field] = previous.get(field, 0) + current.get(field, 0)
    for field in ("errors", "warnings", 'deferred_errors'):
        result[field] = list(dict.fromkeys(previous.get(field, []) + current.get(field, [])))
    if not result.get("after", {}).get("queue", {}).get("pending", 0):
        result["warnings"] = [warning for warning in result["warnings"] if not warning.startswith("Pozostały zadania pending")]
    if not result.get("coverage", {}).get("catalog_pages_pending", 0):
        result["warnings"] = [warning for warning in result["warnings"] if not warning.startswith("Pełny przegląd katalogu jest w toku")]
    result["incomplete_samples"] = (previous.get("incomplete_samples", []) + current.get("incomplete_samples", []))[:20]
    result["background_batches"] = previous.get("background_batches", 0) + 1
    result["stopped_on_errors"] = previous.get("stopped_on_errors", False) or current.get("stopped_on_errors", False)
    if result["errors"]:
        result["queue_complete"] = result["complete"] = False
    return result
