"""Replace resized Audioteka covers safely without changing existing local paths."""

import argparse
import json
import hashlib
import logging
import re
import shutil
import sqlite3
import threading
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from crawl_state import Coverage
from database import Database, download_cover
from events import check_stop, emit
from http_client import Client
from parsers.audioteka import clean_at_cover
from urls import identity

log = logging.getLogger(__name__)


def candidates(db, coverage_path=None):
    """Inspect affected CDN variants and files created by the unified downloader."""
    from PIL import Image
    with db.engine.connect() as connection:
        rows = connection.execute(text("SELECT id,url,cover_url,cover_local_path FROM books WHERE cover_url IS NOT NULL")).mappings().all()
    result = []
    for number, row in enumerate(rows):
        if number % 250 == 0:
            check_stop()
        original = clean_at_cover(row["cover_url"])
        path = Path(row["cover_local_path"]) if row["cover_local_path"] else None
        generated = path and re.fullmatch(r"[0-9a-f]{16}_[0-9a-f]{12}\.jpg", path.name)
        if original == row["cover_url"] and not generated:
            continue
        if path and path.is_file():
            try:
                with Image.open(path) as image:
                    dimensions = image.size
                if min(dimensions) > 300 and original == row["cover_url"]:
                    continue
            except (OSError, ValueError):
                dimensions = None
        else:
            dimensions = None
        if original == row["cover_url"] and dimensions and coverage_path and Path(coverage_path).is_file():
            try:
                with closing(sqlite3.connect(Path(coverage_path).resolve().as_uri()+"?mode=ro", uri=True)) as state:
                    stored = state.execute("SELECT data FROM metadata WHERE site=:site AND product_key=:key",
                                           {"site": "audioteka", "key": identity("audioteka", row["url"])}).fetchone()
            except sqlite3.OperationalError:
                stored = None
            proof = json.loads(stored[0]).get("cover_original", {}) if stored else {}
            if proof.get("url") == original and proof.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest():
                continue
        result.append({**row, "original_url": original, "old_dimensions": dimensions})
    return result


def repair(settings, args, root):
    """Back up and replace a cover only after validating its original resolution."""
    from PIL import Image
    from main import write_json
    root = Path(root)
    db = Database("audioteka", settings["databases"]["audioteka"])
    worker_count = 4
    local = threading.local()
    clients = []
    pool = None
    futures = {}
    def download(item):
        if not hasattr(local, "client"):
            worker_settings = dict(settings, requests_per_second=settings["requests_per_second"] / worker_count)
            local.client = Client(worker_settings)
            clients.append(local.client)
        return download_cover(local.client, item["original_url"], backup / "staging", identity("audioteka", item["url"]))
    ledger = None
    report_path = root / "reports" / "audioteka_cover_repair.json"
    report = {"site": "audioteka", "inserted": 0, "updated": 0, "incomplete": 0,
              "errors": [], "warnings": [], "repaired": [], "original_small": 0, "interrupted": False}
    keep_backup = bool(getattr(args, 'backup', False)) and not getattr(args, 'no_backup', False)
    temporary = None
    if keep_backup:
        backup = root / 'backups' / f'cover_repair_{datetime.now():%Y%m%d_%H%M%S_%f}'
    else:
        (root / 'state').mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='cover_repair_', dir=root / 'state')
        backup = Path(temporary.name)
    report["backup_directory"] = str(backup) if keep_backup else None
    try:
        emit("phase", site="audioteka", phase="Sprawdzanie rozdzielczości okładek")
        items = candidates(db, root / "state" / "audioteka" / "coverage.db")
        report["candidates"] = len(items)
        if args.limit:
            items = items[:args.limit]
        report["selected"] = len(items)
        write_json(report_path, report)
        if not items:
            report["complete"] = True
            return report
        if keep_backup:
            db.backup(backup)
        db.enable_writes()
        # Kopia stanu umożliwia odtworzenie adresów sprzed naprawy.
        state_path = root / "state" / "audioteka" / "coverage.db"
        if keep_backup and state_path.exists():
            with closing(sqlite3.connect(state_path)) as source, closing(sqlite3.connect(backup / "coverage.db")) as target:
                source.backup(target)
        ledger = Coverage(state_path, "audioteka", db.path)
        pool = ThreadPoolExecutor(max_workers=worker_count)
        for index in range(min(worker_count, len(items))):
            futures[index] = pool.submit(download, items[index])
        for number, item in enumerate(items, 1):
            check_stop()
            emit("progress", site="audioteka", done=number-1, total=len(items), inserted=0,
                 updated=report["updated"], incomplete=report["incomplete"], url=item["url"])
            cover = None
            try:
                cover = futures.pop(number-1).result()
                next_index = number-1+worker_count
                if next_index < len(items):
                    futures[next_index] = pool.submit(download, items[next_index])
                if not cover:
                    raise ValueError("Nie udało się pobrać oryginalnej okładki")
                old = item["old_dimensions"]
                if old and (cover["width"] < old[0] or cover["height"] < old[1]):
                    raise ValueError("CDN zwrócił obraz mniejszy od zapisanego")
                if old and (cover["width"], cover["height"]) == tuple(old) and min(old) <= 300:
                    report["original_small"] += 1
                    report["warnings"].append(f"{item['url']}: plik źródłowy również ma {old}; pobrano oryginał bez skalowania")
                target_path = Path(item["cover_local_path"]) if item["cover_local_path"] else root / "media" / "audioteka" / Path(cover["local_path"]).name
                media = (root / "media" / "audioteka").resolve()
                if not target_path.resolve().is_relative_to(media):
                    raise ValueError(f"Ścieżka okładki poza folderem Audioteki: {target_path}")
                target_path.parent.mkdir(parents=True, exist_ok=True)
                previous = backup / "original_files" / str(item["id"]) / target_path.name
                if target_path.exists():
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target_path, previous)
                staged = target_path.with_name(target_path.name + ".repair.tmp")
                shutil.copy2(cover["local_path"], staged)
                with Image.open(staged) as image:
                    image.verify()
                try:
                    with db.engine.begin() as connection:
                        changed = connection.execute(text("""UPDATE books SET cover_url=:original,cover_local_path=:local
                            WHERE id=:id AND cover_url=:old_url AND COALESCE(cover_local_path,'')=:old_path"""),
                            {"original": item["original_url"], "local": str(target_path), "id": item["id"],
                             "old_url": item["cover_url"], "old_path": item["cover_local_path"] or ""})
                        if changed.rowcount != 1:
                            raise ValueError("Rekord zmienił się podczas naprawy; nie podmieniono pliku")
                        staged.replace(target_path)
                except Exception:
                    if previous.exists():
                        shutil.copy2(previous, target_path)
                    staged.unlink(missing_ok=True)
                    raise
                ledger.save_metadata({"url": item["url"], "cover_url": item["original_url"],
                    "cover_original": {"url": item["original_url"], "width": cover["width"], "height": cover["height"],
                                       "sha256": cover["sha256"], "local_path": str(target_path)}})
                report["updated"] += 1
                report["repaired"].append({"id": item["id"], "url": item["url"], "local_path": str(target_path),
                    "old_url": item["cover_url"], "original_url": item["original_url"],
                    "old_dimensions": old, "new_dimensions": [cover["width"], cover["height"]], "sha256": cover["sha256"]})
            except Exception as exc:
                report["errors"].append(f"{item['url']}: {exc}")
                log.error("Naprawa okładki: %s", exc)
            finally:
                if cover:
                    Path(cover["local_path"]).unlink(missing_ok=True)
                if number % 25 == 0 or number == len(items):
                    log.info("Okładki Audioteki: %s/%s; naprawiono=%s, oryginały bez większych wymiarów=%s, błędy=%s", number, len(items), report["updated"], report["original_small"], len(report["errors"]))
                    write_json(report_path, report)
        report["after"] = db.stats()
        report["complete"] = report["updated"] == report["candidates"] and not report["errors"]
        return report
    except KeyboardInterrupt:
        report["interrupted"] = True
        report["complete"] = False
        raise
    finally:
        write_json(report_path, report)
        emit("report", path=str(report_path))
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)
        for future in futures.values():
            if not future.cancelled():
                try:
                    unused = future.result()
                    if unused:
                        Path(unused["local_path"]).unlink(missing_ok=True)
                except BaseException:
                    pass
        if ledger:
            ledger.close()
        for client in clients:
            client.close()
        db.close()
        if temporary:
            temporary.cleanup()


if __name__ == "__main__":
    from main import ROOT, load_settings, run_lock
    from events import STOP_FILE
    parser = argparse.ArgumentParser(description="Naprawa miniaturek Audioteki, z kopią plików i zachowaniem ścieżek")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument('--backup', action='store_true', help='Zachowaj kopię bazy i starych okładek')
    parser.add_argument("--requests-per-second", type=float)
    args = parser.parse_args()
    if args.limit < 0 or (args.requests_per_second is not None and args.requests_per_second <= 0):
        parser.error("Nieprawidłowy limit lub częstotliwość")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings(ROOT / "config.json")
    if args.requests_per_second:
        settings["requests_per_second"] = args.requests_per_second
    with run_lock(ROOT / "scraper.lock"):
        STOP_FILE.unlink(missing_ok=True)
        try:
            result = repair(settings, args, ROOT)
            print(json.dumps({key: result[key] for key in ("candidates", "updated", "incomplete", "errors", "complete")}))
            raise SystemExit(int(bool(result["errors"])))
        except KeyboardInterrupt:
            print("Przerwano bezpiecznie; ponowne uruchomienie pominie naprawione pliki.")
            raise SystemExit(130)
