"""Fill saved titles or collect recommendation data without discovery."""

from database import download_cover
from events import check_stop, emit
from http_client import HTTPError
from parse_books import BookValidationError, NotAudiobook, parse_tags
from recommendations import merge_signals
from sources import TAG_SITES
from urls import identity


def signals_only(args):
    return args.command == "backfill" and (args.tags_only or getattr(args, "recommendations", False))


def fetch_tags(client, site, url):
    response = client.get(url)
    if identity(site, response.url) != identity(site, url):
        raise BookValidationError(f"Przekierowanie poza żądaną książkę: {response.url}")
    return parse_tags(site, response.text, response.url)


def backfill(db, client, ledger, store, archive, args, settings, root, report, fetch_book):
    site = db.site
    tags = site in TAG_SITES
    recommendations = getattr(args, "recommendations", False)
    local_only = getattr(args, "local_only", False)
    recheck = getattr(args, "recheck", False)
    metadata = not (args.tags_only or recommendations)
    if args.tags_only and not tags:
        raise ValueError(f"Uzupełnianie tagów nie jest obsługiwane dla serwisu {site}.")

    book_id = None
    if args.url:
        book_id = db.known.get(identity(site, args.url))
        if book_id is None:
            raise ValueError(f"Brak rekordu dla podanego adresu w bazie {site}.")
    if args.backup and not args.no_backup:
        emit("phase", site=site, phase="Kopia bezpieczeństwa")
        db.backup(root / "backups")
    if metadata:
        db.enable_writes()
    phase = "Eksport z istniejących baz" if local_only else "Uzupełnianie istniejących rekordów"
    emit("phase", site=site, phase=phase)

    if local_only:
        mode = "local"
    elif recommendations:
        mode = "recommendations-recheck" if recheck else "recommendations"
    else:
        mode = "tags" if args.tags_only else "metadata"
    last_id = db.last_book_id()
    cursor = min(store.checkpoint(db, mode), last_id) if book_id is None else 0
    if cursor == last_id:
        cursor = 0
    supplemental = tags or recommendations
    checked = set()
    if supplemental and not (local_only or recheck or args.url):
        checked = store.checked_keys(site, tags_only=not recommendations)
    report.update(attempted=0, downloaded=0, signals=0, tagged=0, without_tags=0, scanned_through=cursor)

    legacy_empty = archive.empty_tag_keys() if tags and not (local_only or args.url) else set()
    consecutive_errors = 0
    while not args.limit or report["attempted"] < args.limit:
        check_stop()
        rows = db.backfill_candidates(cursor, settings["work_batch_size"], metadata=metadata,
                                      supplemental=supplemental, book_id=book_id)
        if not rows:
            finish_scan(store, db, mode, book_id, report)
            break
        work = []
        for row in rows:
            url = db.book_url(row)
            key = identity(site, url or "")
            if key and (row.missing_metadata or supplemental and key not in checked):
                work.append((row, url, key))
        if args.limit:
            work = work[:args.limit - report["attempted"]]
        local = db.stored_signals([row.id for row, _, _ in work]) if supplemental else {}
        cached = archive.get([key for _, _, key in work]) if supplemental else {}
        if mode == "local":
            with store.conn:
                for row, url, key in work:
                    check_stop()
                    data = merge_signals(local[row.id], cached.get(key, {}))
                    report["attempted"] += 1
                    saved = store.save(site, {**data, "url": url}, tags_checked=bool(data["tags"]), only_missing=True)
                    report["signals"] += int(saved)
                    report["tagged"] += int(bool(data["tags"]))
                    report["updated" if saved else "skipped"] += 1
                    if report["attempted"] % 100 == 0:
                        progress(site, report, row.id, last_id, url)
                cursor = work[-1][0].id if args.limit and report["attempted"] >= args.limit else rows[-1].id
                if book_id is None:
                    store.set_checkpoint(db, mode, cursor)
        else:
            for row, url, key in work:
                check_stop()
                report["attempted"] += 1
                progress(site, report, row.id, last_id, url)
                try:
                    data = merge_signals(local.get(row.id, {}), cached.get(key, {}))
                    tags_checked, parsed_card = False, False
                    result = "skipped"
                    if row.missing_metadata or recommendations:
                        report["downloaded"] += 1
                        if recommendations:
                            fetched = fetch_book(client, site, url, include_reviews=False)
                        else:
                            fetched = fetch_book(client, site, url)
                            cover = None
                            if row.missing_cover_row and fetched.get("cover_url") and not args.no_covers:
                                cover = download_cover(client, fetched["cover_url"], root / "media" / site, key)
                            result = db.save(fetched, cover, fill_book_id=row.id)
                            ledger.save_metadata(fetched, only_missing=True)
                        data = merge_signals(data, fetched)
                        tags_checked = "tags" in fetched or bool(data["tags"])
                        parsed_card = True
                    else:
                        if args.url or not data["tags"] and key not in legacy_empty:
                            report["downloaded"] += 1
                            data["tags"] = list(dict.fromkeys(data["tags"] + fetch_tags(client, site, url)))
                        data = {"tags": data["tags"]}
                        tags_checked = True
                    with store.conn:
                        saved = store.save(site, {**data, "url": url}, tags_checked=tags_checked,
                                           recommendations_checked=parsed_card)
                        if book_id is None:
                            store.set_checkpoint(db, mode, row.id)
                    report["signals"] += int(saved)
                    report["tagged"] += int(bool(data["tags"]))
                    report["without_tags"] += int(tags_checked and not data["tags"])
                    report["updated" if saved else result] += 1
                    if (recommendations and parsed_card) or (not recommendations and tags_checked):
                        checked.add(key)
                    consecutive_errors = 0
                except NotAudiobook:
                    report["skipped"] += 1
                    consecutive_errors = 0
                except HTTPError as exc:
                    if exc.status in (404, 410):
                        report["not_found"] += 1
                        consecutive_errors = 0
                    else:
                        report["errors"].append(str(exc))
                        consecutive_errors += 1
                        if exc.status in (403, 429):
                            flag = "stopped_on_access_limit" if exc.status == 403 else "stopped_on_rate_limit"
                            report[flag] = True
                            consecutive_errors = 5
                except Exception as exc:
                    report["errors"].append(f"{url}: {exc}")
                    consecutive_errors += 1

                if consecutive_errors >= 5:
                    report["stopped_on_errors"] = True
                    break
                cursor = row.id
                report["scanned_through"] = cursor
            if not report.get("stopped_on_errors") and (not args.limit or report["attempted"] < args.limit):
                cursor = rows[-1].id
            if book_id is None:
                with store.conn:
                    store.set_checkpoint(db, mode, cursor)

        report["scanned_through"] = cursor
        if report.get("stopped_on_errors"):
            break
        if book_id is not None or cursor >= last_id:
            finish_scan(store, db, mode, book_id, report)
            break


    report["after"] = db.stats()
    if metadata:
        report["metadata_gaps"] = db.audit()
    return report


def finish_scan(store, db, mode, book_id, report):
    if book_id is None:
        with store.conn:
            store.set_checkpoint(db, mode, 0)
    report["scan_finished"] = True


def progress(site, report, book_id, last_id, url):
    emit("progress", site=site, done=book_id, total=last_id, books=report["before"]["books"],
         inserted=0, updated=report["updated"], incomplete=0, url=url)
