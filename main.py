"""One-command incremental updater for all supported databases."""

import argparse
import json
import logging
import os
import sys
import signal
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backfill import backfill, signals_only
from database import Database, download_cover
from crawl_state import Coverage
from events import emit, check_stop, STOP_FILE
from quality import assess
from discovery import Discovery, discover, discover_catalog, page_links
from http_client import Client, HTTPError
from parse_books import BookValidationError, NotAudiobook, parse
from urls import BASES, identity, audioteka_product_redirect
from sources import SOURCES, TAG_SITES, create_database
from state_files import read_state, write_json
from recommendations import ArchivedSignals, Recommendations, recommendations_path

ROOT = Path(__file__).resolve().parent
log = logging.getLogger(__name__)


@contextmanager
def run_lock(path):
    """Prevent two instances from recovering or claiming the same queue."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Inna instancja głównego scrapera już działa.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def load_settings(path):
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    for key in ("requests_per_second", "http_retries", "timeout", "max_listing_pages"):
        if settings[key] <= 0:
            raise ValueError(f"{key} musi być większe od zera")
    if settings["refresh_limit"] < 0:
        raise ValueError("refresh_limit nie może być ujemny")
    settings["databases"] = {site: (path.parent / value.replace("\\", os.sep)).resolve()
                             for site, value in settings["databases"].items()}
    settings["browser_profile"] = path.parent / "browser_profile"
    settings["coverage_database"] = path.parent / "state" / "coverage.db"
    settings.setdefault("catalog_page_budget", 100)
    settings.setdefault("full_catalog_days", 7)
    settings.setdefault("lc_id_batch", 250)
    settings.setdefault("repair_limit", 100)
    settings.setdefault("parallel_services", 4)
    settings.setdefault("backup", False)
    settings.setdefault("news_page_budget", 5)
    settings.setdefault("sitemap_page_budget", 2)
    settings.setdefault("work_batch_size", 100)
    for key in ('catalog_page_budget', 'news_page_budget', 'sitemap_page_budget', 'work_batch_size'):
        if not isinstance(settings[key], int) or settings[key] <= 0:
            raise ValueError(f'{key} musi być dodatnią liczbą całkowitą')
    return settings


def fetch_book(client, site, url, *, include_reviews=True, all_recommendations=False):
    requested = url
    if site == "wolnelektury":
        requested = BASES[site] + "/api/books/" + identity(site, url) + "/?format=json"
    response = client.get(requested, render=site == "empik")
    if site in SOURCES:
        data = parse(site, response.text, response.url)
        requested_id = identity(site, url)
        if requested_id != identity(site, response.url) and requested_id not in data.get("source_ids", []):
            raise BookValidationError(f"Przekierowanie poza żądany audiobook: {url}")
        from product_validation import validate_download
        validate_download(site, response.text, response.url, url, data)
        if site == 'bookbeat':
            from dynamic_metadata import bookbeat_related
            bookbeat_related(client, data, all_pages=True) if all_recommendations else bookbeat_related(client, data)
        if site == 'bookbeat' and include_reviews:
            from dynamic_metadata import bookbeat_reviews
            bookbeat_reviews(client, data)
            if data.get('review_coverage', {}).get('error'):
                log.warning('[bookbeat] Nie udało się odczytać opinii %s: %s', url, data['review_coverage']['error'])
        return data
    if identity(site, response.url) != identity(site, url) and not (site == "audioteka" and audioteka_product_redirect(url, response.url)):
        raise ValueError(f"Przekierowanie poza żądaną książkę: {response.url}")
    data = parse(site, response.text, response.url)
    from product_validation import validate_download
    return validate_download(site, response.text, response.url, url, data)


def run_site(site, settings, args, state):
    if args.command == "repair-covers":
        from repair_audioteka_covers import repair
        return repair(settings, args, ROOT)
    report = {"site": site, "inserted": 0, "updated": 0, "skipped": 0, "not_found": 0, "incomplete": 0, "incomplete_samples": [], "errors": [], "warnings": []}
    args.partial_report = report
    if args.command != "backfill" and site in SOURCES and not Path(settings["databases"][site]).exists():
        create_database(site, settings["databases"][site])
    review_index = Path(settings["coverage_database"]).parent / f"{site}_reviews.db" if args.command == "update" else None
    emit("phase", site=site, phase="Odczyt bazy")
    db = Database(site, settings["databases"][site], review_index=review_index, read_only=signals_only(args))
    client = Client(settings)
    ledger = Coverage(settings["coverage_database"], site, db.path) if args.command == "update" else None
    recommendations = None
    try:
        report["before"] = db.stats()
        emit('inventory', site=site, books=report['before']['books'])
        if args.command == "backfill":
            recommendations = Recommendations(recommendations_path(settings))
            if not signals_only(args):
                ledger = Coverage(settings["coverage_database"], site, db.path)
            with closing(ArchivedSignals(settings["coverage_database"], site)) as archive:
                return backfill(db, client, ledger, recommendations, archive, args, settings, ROOT, report, fetch_book)
        if ledger:
            ledger.apply_aliases(db.known)
        found = None
        background = getattr(args, "background", False)
        new_only = getattr(args, "new_only", False) and not args.url
        retry_cutoff = settings.get("background_retry_cutoff")
        if background:
            emit("phase", site=site, phase="Uzupełnianie w tle")
            found = Discovery(observer=ledger.discover)
            if getattr(args, 'background_catalog', False) and ledger.checkpoint('sitemaps_pending', []):
                from discovery import discover_sitemaps
                discover_sitemaps(site, client, found, max_maps=settings.get('sitemap_page_budget', 2), ledger=ledger)
            if getattr(args, "background_catalog", False) and ledger.pending_page(retry_cutoff):
                discover_catalog(site, client, found, ledger, settings)
            candidates = [entry["url"] for entry in found.urls.values()]
            report.update(listing_pages=found.pages, discovered=len(found.urls), catalog_complete=not ledger.has_pending_pages())
            report["errors"].extend(found.errors)
            report["warnings"].extend(found.warnings)
        elif args.url:
            candidates = [args.url]
        elif args.resume or args.command in ("stats", "audit"):
            candidates = []
        else:
            full_catalog = args.full_catalog
            if ledger and not getattr(args, "fast", False):
                full_catalog = full_catalog or ledger.has_pending_pages() or time.time() - ledger.checkpoint("last_full_catalog", 0) >= settings["full_catalog_days"] * 86400
            emit("phase", site=site, phase="Wykrywanie linków", full_catalog=full_catalog)
            found = discover(site, client, settings, db.known, full_catalog,
                             observer=ledger.discover if ledger else None, ledger=ledger)
            candidates = [entry["url"] for entry in found.urls.values()]
            report.update({"maps": found.maps, "listing_pages": found.pages, "discovered": len(found.urls)})
            report["errors"].extend(found.errors)
            report["warnings"].extend(found.warnings)
            report["catalog_complete"] = found.catalog_complete
        report["new_candidates"] = sum(identity(site, url) not in db.known for url in candidates)
        log.info("[%s] Zebrano %s linków, brakujących książek: %s", site, len(candidates), report["new_candidates"])
        if args.command == "diagnose":
            samples = sorted(candidates, key=lambda u: identity(site, u) in db.known)[:args.samples]
            report["samples"] = []
            for url in samples:
                try:
                    data = fetch_book(client, site, url)
                    quality = assess(site, data)
                    report["samples"].append({"data": data, "quality": quality, "cookies": client.cookie_status if site == "empik" else None})
                    if not quality["complete"]:
                        report["errors"].append(f"Niepełne dane {url}: {quality['missing_required']}")
                except NotAudiobook as exc:
                    report["warnings"].append(str(exc))
                except Exception as exc:
                    report["errors"].append(f"Test parsera {url}: {exc}")
            return report
        if args.command == "audit":
            report["metadata_gaps"] = db.audit()
            print(f"{site}: braki metadanych={report['metadata_gaps']}")
            return report
        if args.command == "stats":
            return report
        if getattr(args, 'backup', False) and not args.no_backup:
            emit("phase", site=site, phase="Kopia bezpieczeństwa")
            db.backup(ROOT / "backups")
        db.enable_writes()
        db.recover()
        queue_candidates = [url for url in candidates if not ledger or args.url or ledger.due(url)]
        report["queued_new"] = db.enqueue(queue_candidates, refresh=bool(args.url), priority=100 if args.url else 80)
        if ledger:
            for candidate in candidates:
                ledger.discover(candidate)
            ledger.reconcile(db.known)
            repair_work = [] if args.url else ledger.work(db.known, retry_cutoff)
            if new_only:
                repair_work = [url for url in repair_work if identity(site, url) not in db.known]
            report["queued_repair"] = db.enqueue(repair_work, refresh=not new_only, priority=60)
            if background:
                report["queued_inherited_repair"] = db.enqueue(args.background_urls, refresh=True, priority=30)
                ledger.set_checkpoint("repair_cursor", args.background_repair_cursor)
            elif not args.resume and not args.url and not new_only:
                repairs, repair_cursor = db.repair_urls(ledger.checkpoint("repair_cursor", 0), settings["repair_limit"])
                report["queued_inherited_repair"] = db.enqueue([url for url in repairs if ledger.due(url)], refresh=True, priority=30)
                ledger.set_checkpoint("repair_cursor", repair_cursor)
            if site == "lubimyczytac" and not args.resume and not args.url and not new_only and settings["lc_id_batch"] > 0:
                maximum = max((int(key) for key in db.known if key.isdigit()), default=0)
                target = max([maximum, ledger.checkpoint("lc_id_target", 0)] + [int(identity(site, url)) for url in candidates])
                cursor_id = ledger.checkpoint("lc_id_cursor", maximum)
                requested_range = [getattr(args, "lc_id_start", None), getattr(args, "lc_id_end", None)]
                if requested_range[0] is not None and ledger.checkpoint("lc_requested_range") != requested_range:
                    cursor_id = args.lc_id_start
                    ledger.set_checkpoint("lc_requested_range", requested_range)
                if getattr(args, "lc_id_end", None) is not None:
                    target = args.lc_id_end
                urls, end = db.id_gap_urls(cursor_id, target, settings["lc_id_batch"])
                report["queued_id_gaps"] = db.enqueue(urls, priority=20)
                for url in urls:
                    ledger.discover(url)
                ledger.set_checkpoint("lc_id_cursor", end)
                ledger.set_checkpoint("lc_id_target", target)
                report["id_scan"] = {"cursor": end, "target": target, "remaining_ids": max(0, target - end)}
        site_state = state.setdefault(site, {})
        if site_state.get("database") != str(db.path):
            site_state.clear()
            site_state["database"] = str(db.path)
        refresh_limit = settings["refresh_limit"] if args.refresh_limit is None else args.refresh_limit
        if args.resume and args.refresh_limit is None:
            refresh_limit = 0
        if new_only:
            refresh_limit = 0
        refresh, cursor = db.refresh_urls(site_state.get("refresh_cursor", 0), refresh_limit)
        remaining_refresh = {identity(site, url) for url in refresh}
        report["queued_refresh"] = db.enqueue(refresh, refresh=True, priority=10)
        items = db.pending()
        if ledger and not args.url:
            items = [item for item in items if ledger.due(item["url"], retry_cutoff)]
        if new_only:
            items = [item for item in items if item["type"] != "book" or identity(site, item["url"]) not in db.known]
        if background:
            items = [item for item in items if item["id"] not in args.attempted_tasks]
        if args.limit:
            items = items[:args.limit]
        if args.url:
            items = [item for item in db.pending() if identity(site, item["url"]) == identity(site, args.url)][:1]
        seen_tasks = {item["id"] for item in items}
        consecutive_errors = 0
        for number, item in enumerate(items, 1):
            if new_only and item["type"] == "book" and identity(site, item["url"]) in db.known:
                continue
            if ledger and not args.url and not ledger.due(item["url"], retry_cutoff):
                continue
            emit("progress", site=site, done=number - 1, total=len(items), books=report['before']['books'] + report['inserted'], skipped=report['skipped'], inserted=report["inserted"] + getattr(args, "totals", {}).get("inserted", 0), updated=report["updated"] + getattr(args, "totals", {}).get("updated", 0), incomplete=report["incomplete"] + getattr(args, "totals", {}).get("incomplete", 0), url=item["url"])
            if hasattr(args, "attempted_tasks"):
                args.attempted_tasks.add(item["id"])
            db.mark(item["id"], "processing")
            try:
                if item["type"] != "book":
                    # Stare zadania list nie są książkami i nie mogą zniknąć bez odczytu.
                    response = client.get(item["url"], browser_fallback=site == "empik")
                    links, _ = page_links(site, response.text, response.url)
                    if not links:
                        raise ValueError("Brak książek na starej liście")
                    db.enqueue(links)
                    if not args.limit:
                        for extra in db.pending():
                            if extra["id"] not in seen_tasks:
                                items.append(extra)
                                seen_tasks.add(extra["id"])
                    report["skipped"] += 1
                elif identity(site, item["url"]) is None:
                    db.mark(item["id"], "skipped")
                    report["skipped"] += 1
                    continue
                else:
                    data = fetch_book(client, site, item["url"])
                    if data.get("recommendation_coverage", {}).get("error"):
                        report["warnings"].append(f"Rekomendacje {item['url']}: {data['recommendation_coverage']['error']}")
                    review_coverage = data.get('review_coverage') or {}
                    if review_coverage.get('error'):
                        warning = 'Nie udało się pobrać części opinii; metadane książek zapisano, szczegóły w pomocniczych metadanych'
                        if warning not in report['warnings']:
                            report['warnings'].append(warning)
                    elif review_coverage and not review_coverage.get('complete'):
                        warning = 'Opinie BookBeat: pobrano najnowszą partię; pełne archiwum wymaga dalszej paginacji'
                        if warning not in report['warnings']:
                            report['warnings'].append(warning)
                    requested_key = identity(site, item["url"])
                    target_key = identity(site, data["url"])
                    if requested_key != target_key and requested_key in db.known and target_key not in db.known:
                        db.known[target_key] = db.known[requested_key]
                    cover = None
                    cover_needed = not args.no_covers and data.get("cover_url") and db.needs_cover(data["url"])
                    if cover_needed:
                        cover = download_cover(client, data["cover_url"], ROOT / "media" / site, identity(site, data["url"]))
                    result = db.save(data, cover)
                    if requested_key != target_key:
                        db.known[requested_key] = db.known[target_key]
                        if ledger:
                            ledger.mark(item["url"], "redirected", {"redirect_url": data["url"]})
                    if ledger:
                        ledger.save_metadata(data)
                        if not report.get("recommendations_error"):
                            try:
                                if recommendations is None:
                                    recommendations = Recommendations(recommendations_path(settings))
                                with recommendations.conn:
                                    recommendations.save(site, data, tags_checked="tags" in data,
                                                         recommendations_checked=not data.get("recommendation_coverage", {}).get("error"),
                                                         replace_labels=True)
                            except (OSError, sqlite3.Error) as exc:
                                report["recommendations_error"] = str(exc)
                                report["warnings"].append(f"Zapis danych do rekomendacji: {exc}")
                    report[result] += 1
                    quality = assess(site, data)
                    if cover_needed and not cover:
                        quality["missing_required"].append("cover_local_file")
                        quality["complete"] = False
                    if not quality["complete"]:
                        if requested_key != target_key:
                            # Ponowienie dotyczy adresu docelowego, a alias jest już rozpoznany.
                            db.mark(item["id"], "completed")
                            db.enqueue([data["url"]], refresh=True, priority=60)
                        else:
                            db.mark(item["id"], "incomplete", failed=True)
                        if ledger:
                            ledger.mark(data["url"], "incomplete", quality)
                        report["incomplete"] += 1
                        # Brak metadanych nie oznacza awarii pobierania całego serwisu.
                        if len(report["incomplete_samples"]) < 20:
                            report["incomplete_samples"].append({"url": item["url"], "missing_required": quality["missing_required"]})
                        consecutive_errors = 0
                        remaining_refresh.discard(identity(site, item["url"]))
                    elif ledger:
                        ledger.mark(data["url"], "verified", quality)
                if item["type"] != "book" or quality["complete"]:
                    db.mark(item["id"], "scraped" if site == "empik" else "completed")
                remaining_refresh.discard(identity(site, item["url"]))
                consecutive_errors = 0
            except NotAudiobook:
                db.mark(item["id"], "skipped")
                remaining_refresh.discard(identity(site, item["url"]))
                report["skipped"] += 1
                if ledger:
                    ledger.mark(item["url"], "skipped", {"reason": "not_audiobook"})
                consecutive_errors = 0
            except HTTPError as exc:
                if exc.status in (404, 410):
                    db.mark(item["id"], "archived_error", failed=True)
                    remaining_refresh.discard(identity(site, item["url"]))
                    report["not_found"] += 1
                    if ledger:
                        ledger.mark(item["url"], "not_found", {"http": exc.status})
                    consecutive_errors = 0
                else:
                    db.mark(item["id"], "pending" if item["retry_count"] < 2 else "error", failed=True)
                    broken_card = site == 'woblink' and '/audiobook/--' in item['url'] and exc.status == 500
                    if broken_card:
                        report.setdefault('deferred_errors', []).append(str(exc))
                        warning = 'Woblink: wadliwe karty bez tytułu odłożono do ponowienia; pozostałe książki są pobierane dalej'
                        if warning not in report['warnings']:
                            report['warnings'].append(warning)
                    else:
                        report["errors"].append(str(exc))
                    if ledger:
                        ledger.mark(item["url"], "error", {"message": str(exc)})
                    # Puste slugi Woblinka to wadliwe pojedyncze karty, nie awaria serwisu.
                    consecutive_errors = 0 if broken_card else consecutive_errors + 1
                    if exc.status == 403:
                        report['stopped_on_access_limit'] = True
                        consecutive_errors = 5
                    if exc.status == 429:
                        report["stopped_on_rate_limit"] = True
                        consecutive_errors = 5
            except BookValidationError as exc:
                report.setdefault("identity_rejections", []).append({"url": item["url"], "reason": str(exc)})
                db.mark(item["id"], "error", failed=True)
                report["errors"].append(f"{item['url']}: {exc}")
                if ledger:
                    ledger.mark(item["url"], "error", {"message": str(exc), "kind": "validation"})
                log.error("[%s] Pominięto niezgodną stronę: %s", site, item["url"])
                consecutive_errors = 0
            except KeyboardInterrupt:
                db.mark(item["id"], "pending")
                raise
            except Exception as exc:
                db.mark(item["id"], "pending" if item["retry_count"] < 2 else "error", failed=True)
                report["errors"].append(f"{item['url']}: {exc}")
                if ledger:
                    ledger.mark(item["url"], "error", {"message": str(exc)})
                log.error("[%s] %s: %s", site, item["url"], exc)
                consecutive_errors += 1
            if number % 25 == 0 or number == len(items):
                log.info("[%s] %s/%s: nowe=%s, odświeżone=%s, błędy=%s", site, number, len(items), report["inserted"], report["updated"], len(report["errors"]))
            if consecutive_errors >= 5:
                report["stopped_on_errors"] = True
                report["errors"].append("Serwis odmawia dostępu (HTTP 403); zapisano kolejkę do późniejszego wznowienia" if report.get('stopped_on_access_limit') else "Serwis ogranicza żądania (HTTP 429); wstrzymano pobieranie i zapisano kolejkę" if report.get("stopped_on_rate_limit") else "Przerwano ten serwis po 5 kolejnych błędach; kolejka zostaje do wznowienia")
                break
        # Błąd sitemap nie zatrzymuje odświeżania poprawnie pobranej paczki.
        if not remaining_refresh:
            site_state["refresh_cursor"] = cursor
        site_state["last_run"] = datetime.now(timezone.utc).isoformat()
        if report["incomplete"]:
            report["warnings"].append(f"Zapisano {report['incomplete']} rekordów z brakami danych; ponowne próby są odłożone i nie blokują kolejki")
        report["after"] = db.stats()
        report["metadata_gaps"] = db.audit()
        if ledger:
            report["coverage"] = ledger.summary()
        report["queue_complete"] = not any(report["after"]["queue"].get(status, 0) for status in ("pending", "processing", "error", "incomplete")) and not report["errors"] and not report.get("coverage", {}).get("unresolved", 0)
        report["complete"] = report["queue_complete"] and not report["metadata_gaps"]["incomplete_books"] and not report.get("coverage", {}).get("catalog_pages_pending", 0)
        if report["after"]["queue"].get("pending", 0):
            report["warnings"].append("Pozostały zadania pending; zostaną wznowione przy następnym uruchomieniu")
        if site == "nexto":
            report["source_archive"] = True
            report["complete"] = False
        return report
    finally:
        try:
            client.close()
        finally:
            if recommendations:
                recommendations.close()
            if ledger:
                ledger.close()
            db.close()


def run_cycle(settings, args):
    state_path = ROOT / "state.json"
    state = read_state(state_path)
    sites = list(dict.fromkeys(args.services or [site for site in settings["databases"] if SOURCES.get(site, {}).get("active", True)]))
    for site in sites:
        checkpoint = ROOT / "state" / site / "run.json"
        if args.command == "update" and checkpoint.exists():
            state.update(read_state(checkpoint, {site: state.get(site, {})}))
    workers = min(getattr(args, "workers", None) or settings.get("parallel_services", 1), len(sites))
    emit("schedule", sites=sites, workers=workers)
    interrupted = False
    if workers > 1 or args.command == 'update' and not args.url and not args.limit:
        from parallel import run_parallel
        log.info("Wybrano %s serwisów; jednocześnie do %s; pozostałe czekają na wolny proces", len(sites), workers)
        reports, interrupted = run_parallel(settings, args, state, ROOT, workers)
    else:
        reports = []
        for site in sites:
            check_stop()
            log.info("=== %s ===", site.upper())
            site_settings = settings
            if args.command in ("update", "backfill") and not signals_only(args):
                from parallel import service_settings
                site_settings = service_settings(settings, site)
            try:
                report = run_site(site, site_settings, args, state)
            except KeyboardInterrupt:
                if args.command == "update":
                    write_json(ROOT / "state" / site / "run.json", {site: state.get(site, {})})
                    write_json(state_path, state)
                if args.command != "backfill":
                    raise
                report = getattr(args, "partial_report", None) or {"site": site, "errors": [], "warnings": []}
                report.update(interrupted=True)
                interrupted = True
            except Exception as exc:
                log.exception("[%s] Nie udało się wykonać aktualizacji", site)
                report = {"site": site, "errors": [str(exc)], "warnings": []}
            reports.append(report)
            emit("summary", **report)
            if interrupted:
                break
            if args.command == "update":
                write_json(ROOT / "state" / site / "run.json", {site: state.get(site, {})})
                write_json(state_path, state)
    for report in reports:
        site = report["site"]
        for warning in report["warnings"]:
            log.warning("[%s] %s", site, warning)
        for error in report["errors"]:
            log.error("[%s] %s", site, error)
        if args.command == "stats" and "before" in report:
            print(f"{site}: książki={report['before']['books']}, kolejka={report['before']['queue']}")
        elif args.command == "backfill":
            print(f"{site}: sprawdzono={report.get('attempted', 0)}, pobrano={report.get('downloaded', 0)}, "
                  f"z sygnałami={report.get('signals', 0)}, z tagami={report.get('tagged', 0)}, "
                  f"bez tagów={report.get('without_tags', 0)}, "
                  f"niedostępne={report.get('not_found', 0)}, błędy={len(report['errors'])}")
        else:
            print(f"{site}: nowe={report.get('inserted', 0)}, odświeżone={report.get('updated', 0)}, "
                  f"kandydaci={report.get('new_candidates', 0)}, braki={report.get('incomplete', 0)}, błędy={len(report['errors'])}")
    output = ROOT / "reports" / f"{args.command}_{datetime.now():%Y%m%d_%H%M%S}.json"
    write_json(output, {"timestamp": datetime.now(timezone.utc).isoformat(), "command": args.command, "services": reports, "interrupted": interrupted, "workers": workers})
    emit("report", path=str(output))
    print(f"Raport: {output}")
    return 130 if interrupted else int(any(r["errors"] for r in reports))


def main():
    parser = argparse.ArgumentParser(description="Aktualizacja baz audiobooków jednym poleceniem")
    parser.add_argument("command", nargs="?", default="update", choices=("update", "diagnose", "stats", "audit", "repair-covers", "backfill"))
    parser.add_argument("--services", nargs="+", choices=list(BASES))
    parser.add_argument("--workers", type=int, choices=range(1, len(BASES) + 1), help="Maksymalna liczba równoległych serwisów; 1 = kolejno")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--limit", type=int, default=0, help="Maksymalna liczba zadań na serwis; 0 = wszystkie")
    parser.add_argument("--refresh-limit", type=int, help="Liczba istniejących książek do odświeżenia na serwis")
    parser.add_argument("--repair-limit", type=int, help="Liczba starych niepełnych rekordów do naprawy na serwis")
    parser.add_argument("--resume", action="store_true", help="Tylko wznowienie kolejki, bez wykrywania linków")
    parser.add_argument("--full-catalog", action="store_true", help="Przegląd katalogów wszystkich serwisów z zapisem punktu wznowienia")
    parser.add_argument("--new-only", action="store_true", help="Pobieraj wyłącznie nieznane książki, bez napraw i odświeżania starych")
    parser.add_argument("--fast", action="store_true", help="Pomiń okresowy przegląd katalogu")
    parser.add_argument("--headless", action="store_true", help="Ukryj przeglądarkę Empiku")
    parser.add_argument("--catalog-page-budget", type=int, help="Liczba stron katalogu na serwis w przebiegu")
    parser.add_argument("--lc-id-start", type=int, help="Zacznij historyczny skan ID LC po tym numerze")
    parser.add_argument("--lc-id-end", type=int, help="Górna granica skanu ID LC")
    parser.add_argument("--lc-id-batch", type=int, help="Liczba ID LC sprawdzanych w przebiegu")
    parser.add_argument("--url", help="Pobierz jeden adres; serwis zostanie rozpoznany automatycznie")
    parser.add_argument("--samples", type=int, default=1, help="Testowane książki na serwis w diagnose")
    backups = parser.add_mutually_exclusive_group()
    backups.add_argument("--backup", action="store_true", help="Utwórz kopię baz przed aktualizacją (domyślnie wyłączone)")
    backups.add_argument("--no-backup", action="store_true")
    parser.add_argument("--no-covers", action="store_true")
    signals = parser.add_mutually_exclusive_group()
    signals.add_argument("--complete", action="store_true", help="Zweryfikuj wszystkie istniejące rekordy i uzupełnij metadane, kategorie, tagi oraz rekomendacje")
    signals.add_argument("--tags-only", action="store_true", help="W trybie backfill uzupełniaj tylko tagi LubimyCzytać, Storytela, Empiku i Woblinka")
    signals.add_argument("--recommendations", action="store_true", help="W trybie backfill zbieraj dane do rekomendacji")
    parser.add_argument("--local-only", action="store_true", help="Eksportuj dane do rekomendacji z lokalnych baz i zapisanych metadanych")
    parser.add_argument("--recheck", action="store_true", help="Pobierz ponownie również wcześniej sprawdzone karty w trybie rekomendacji lub samych tagów")
    parser.add_argument("--sync-categories", action="store_true", help="W trybie rekomendacji zapisz sprawdzone kategorie także do baz serwisów")
    parser.add_argument("--interval-hours", type=float, help="Powtarzaj pełny cykl co N godzin")
    args = parser.parse_args()
    if args.complete:
        if args.command != "backfill" or args.local_only:
            parser.error("--complete wymaga backfill z pobieraniem kart")
        args.recommendations = args.recheck = args.sync_categories = True
    if args.catalog_page_budget is not None and args.catalog_page_budget <= 0:
        parser.error("--catalog-page-budget musi być większe od zera")
    if any(value is not None and value < 0 for value in (args.lc_id_start, args.lc_id_end, args.lc_id_batch)):
        parser.error("Zakres i paczka ID nie mogą być ujemne")
    if args.lc_id_start is not None and args.lc_id_end is not None and args.lc_id_start >= args.lc_id_end:
        parser.error("Początek zakresu musi być mniejszy niż koniec")
    if args.limit < 0 or args.samples < 0 or (args.refresh_limit is not None and args.refresh_limit < 0):
        parser.error("Limity nie mogą być ujemne")
    if args.repair_limit is not None and args.repair_limit < 0:
        parser.error("--repair-limit nie może być ujemny")
    if args.interval_hours is not None and (args.interval_hours <= 0 or args.command != "update"):
        parser.error("--interval-hours wymaga update i wartości większej od zera")
    if args.command == "repair-covers":
        if args.url:
            parser.error("Naprawa okładek nie przyjmuje --url")
        args.services = ["audioteka"]
        args.workers = 1
    if args.url:
        sites = [site for site in BASES if identity(site, args.url)]
        if len(sites) != 1:
            parser.error("Nie rozpoznano adresu książki")
        args.services = sites
        args.refresh_limit = 0
        args.limit = 1
    if (args.tags_only or args.recommendations) and args.command != "backfill":
        parser.error("--tags-only i --recommendations wymagają trybu backfill")
    if args.local_only and not args.recommendations:
        parser.error("--local-only wymaga --recommendations")
    if args.recheck and (not (args.recommendations or args.tags_only) or args.local_only):
        parser.error("--recheck wymaga --recommendations lub --tags-only z pobieraniem kart")
    if args.sync_categories and (not args.recommendations or args.local_only):
        parser.error("--sync-categories wymaga --recommendations z pobieraniem kart")
    if args.tags_only:
        args.services = args.services or list(TAG_SITES)
        if any(site not in TAG_SITES for site in args.services):
            parser.error("--tags-only obsługuje wyłącznie LubimyCzytać, Storytel, Empik i Woblink")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(ROOT / "logs" / "scraper.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ])
    try:
        settings = load_settings(args.config.resolve())
        if args.repair_limit is not None:
            settings["repair_limit"] = args.repair_limit
        if args.catalog_page_budget is not None:
            settings["catalog_page_budget"] = args.catalog_page_budget
        if args.lc_id_batch is not None:
            settings["lc_id_batch"] = args.lc_id_batch
        if args.headless:
            settings["browser_headless"] = True
        if hasattr(signal, "SIGBREAK"):
            def stop_on_break(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGBREAK, stop_on_break)
        with run_lock(ROOT / "scraper.lock"):
            if os.environ.get("NOKTURN_GUI_EVENTS") != "1":
                STOP_FILE.unlink(missing_ok=True)
            while True:
                check_stop()
                code = run_cycle(settings, args)
                if args.interval_hours is None or code == 130:
                    return code
                log.info("Kolejny cykl za %s godzin", args.interval_hours)
                deadline = time.monotonic() + args.interval_hours * 3600
                while time.monotonic() < deadline:
                    check_stop()
                    time.sleep(min(1, deadline - time.monotonic()))
    except KeyboardInterrupt:
        print("Przerwano. Zapisane rekordy i kolejka zostają do wznowienia.")
        return 130
    except Exception as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
