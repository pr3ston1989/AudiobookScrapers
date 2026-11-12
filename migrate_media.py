"""Relocate existing cover folders and rebase paths without changing DB schemas."""

import sqlite3
import os
from pathlib import Path
from main import ROOT, load_settings, run_lock, write_json


def migrate():
    workspace = ROOT.parent.resolve()
    locations = {"audioteka": ("audioteka.com", "media"), "storytel": ("storytel.com", "media"),
                 "empik": ("empik.com", "covers"), "lubimyczytac": ("lubimyczytac", "media")}
    settings = load_settings(ROOT / "config.json")
    report = {}
    with run_lock(ROOT / "scraper.lock"):
        for site, (folder, subfolder) in locations.items():
            source = (workspace / folder / subfolder).resolve()
            target = (ROOT / "media" / site).resolve()
            if not source.is_relative_to(workspace) or not target.is_relative_to(ROOT):
                raise ValueError("Ścieżka mediów poza workspace")
            target.parent.mkdir(exist_ok=True)
            if source.exists():
                if target.exists():
                    raise FileExistsError(f"Nie nadpisuję folderu: {target}")
                source.rename(target)
            if not target.exists():
                raise FileNotFoundError(f"Brak okładek: {source}")
            table, field = ("covers", "local_path") if site == "lubimyczytac" else ("books", "cover_local_path")
            conn = sqlite3.connect(settings["databases"][site])
            try:
                rows = conn.execute(f'SELECT id,"{field}" FROM {table} WHERE "{field}" IS NOT NULL').fetchall()
                updates = []
                for book_id, old in rows:
                    normalized = old.replace("/", "\\")
                    if site == "empik":
                        relative = Path(normalized).name
                    elif normalized.lower().startswith("media\\"):
                        relative = normalized[6:]
                    elif str(target).lower() in normalized.lower():
                        continue
                    else:
                        # Nie zgadujemy położenia nietypowych ścieżek.
                        continue
                    new_path = os.path.abspath(target / relative)
                    if not Path(new_path).is_relative_to(target):
                        raise ValueError(f"Nieprawidłowa ścieżka okładki: {old}")
                    updates.append((new_path, book_id))
                conn.executemany(f'UPDATE {table} SET "{field}"=? WHERE id=?', updates)
                conn.commit()
                report[site] = {"source": str(source), "target": str(target), "paths_rebased": len(updates),
                                "sample_paths_exist": [Path(path).is_file() for path, _ in updates[:10]]}
                print(site, len(updates), "paths rebased", flush=True)
            finally:
                conn.close()
            write_json(ROOT / "reports" / "media_migration.json", report)


if __name__ == "__main__":
    migrate()
