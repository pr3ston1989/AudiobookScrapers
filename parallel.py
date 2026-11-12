"""Isolated per-service workers with streamed output and cooperative stopping."""

import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from backfill import signals_only
from recommendations import recommendations_path
from sources import SOURCES
from events import emit


def service_settings(settings, site):
    """Split legacy shared crawl state before workers acquire independent writers."""
    result = dict(settings)
    legacy = Path(settings["coverage_database"])
    target = legacy.parent / site / "coverage.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    if legacy.exists() and not target.exists():
        temporary = target.with_suffix(".db.tmp")
        temporary.unlink(missing_ok=True)
        source = sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(temporary)
        try:
            tables = source.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            for name, sql in tables:
                destination.execute(sql)
                columns = [row[1] for row in source.execute(f'PRAGMA table_info("{name}")')]
                if "site" not in columns:
                    continue
                rows = source.execute(f'SELECT * FROM "{name}" WHERE site=?', (site,))
                placeholders = ",".join("?" for _ in columns)
                destination.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)
            destination.commit()
        finally:
            source.close()
            destination.close()
        temporary.replace(target)
    result["recommendations_database"] = recommendations_path(settings)
    result["coverage_database"] = target
    return result


def run_parallel(settings, args, state, root, workers):
    """Run independent services without sharing SQLite connections or cursors."""
    root = Path(root)
    jobs_root = root / "state" / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    messages = queue.Queue()
    requested = list(dict.fromkeys(args.services or [site for site in settings["databases"] if SOURCES.get(site, {}).get("active", True)]))
    database_paths = [Path(settings["databases"][site]).resolve() for site in requested]
    if len(set(database_paths)) != len(database_paths):
        raise ValueError("Równoległe serwisy muszą wskazywać różne pliki baz danych.")
    waiting = list(requested)
    active, reports = {}, {}
    early_finished, continued, attempted = set(), set(), {}
    cycle_started = time.time()
    interrupted = False
    stop_file = Path(os.environ.get("NOKTURN_STOP_FILE", root / "stop.request"))
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")

    def read_output(site, process):
        for line in process.stdout:
            messages.put(line.rstrip("\r\n"))
        process.stdout.close()

    with tempfile.TemporaryDirectory(prefix="cycle_", dir=jobs_root) as directory:
        directory = Path(directory)
        peers = directory / "peers"
        peers.mkdir()
        for site in requested:
            (peers / f"{site}.running").touch()
            emit("phase", site=site, phase="Oczekuje na wolny proces")
        while waiting or active:
            try:
                if stop_file.exists():
                    interrupted = True
                    waiting.clear()
                while waiting and len(active) < workers and not interrupted:
                    site = waiting.pop(0)
                    emit("phase", site=site, phase="Uruchamianie")
                    result_path = directory / f"{site}.result.json"
                    result_path.unlink(missing_ok=True)
                    try:
                        isolated = (service_settings(settings, site)
                                    if args.command in ("update", "backfill") and not signals_only(args) else settings)
                    except Exception as exc:
                        reports[site] = {"site": site, "errors": [str(exc)], "warnings": []}
                        (peers / f"{site}.running").unlink(missing_ok=True)
                        continue
                    job = {"site": site, "settings": isolated, "args": vars(args),
                           "state": {site: state.get(site, {})}, "root": root, "result": result_path, "peers": peers,
                           "cycle_started": cycle_started, "attempted_tasks": attempted.get(site, []),
                           "previous_report": reports.get(site) if site in continued else None}
                    job_path = directory / f"{site}.job.json"
                    job_path.write_text(json.dumps(job, default=str, ensure_ascii=False), encoding="utf-8")
                    env = dict(os.environ, PYTHONIOENCODING="utf-8", NOKTURN_STOP_FILE=str(stop_file))
                    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    try:
                        process = subprocess.Popen([str(executable), "-u", str(Path(__file__).with_name("worker.py")), str(job_path)],
                                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                                                   env=env, cwd=root, creationflags=flags)
                    except OSError as exc:
                        reports[site] = {"site": site, "errors": [str(exc)], "warnings": []}
                        (peers / f"{site}.running").unlink(missing_ok=True)
                        continue
                    reader = threading.Thread(target=read_output, args=(site, process), daemon=True)
                    reader.start()
                    active[site] = (process, reader, result_path)
                if not waiting:
                    (peers / "ready").touch()
                try:
                    print(messages.get(timeout=0.1), flush=True)
                except queue.Empty:
                    pass
                for site, (process, reader, result_path) in list(active.items()):
                    if process.poll() is None:
                        continue
                    reader.join(timeout=1)
                    if result_path.exists():
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                        reports[site] = result["report"]
                        attempted[site] = result.get("attempted_tasks", [])
                        if result.get("initial_pass_only"):
                            early_finished.add(site)
                        state.update(result["state"])
                        interrupted = interrupted or result.get("interrupted", False)
                        if result.get("interrupted", False):
                            waiting.clear()
                            stop_file.write_text("stop", encoding="ascii")
                    else:
                        reports[site] = {"site": site, "errors": [f"Proces zakończył się bez raportu (kod {process.returncode})"], "warnings": []}
                    if args.command == "update":
                        from main import write_json
                        write_json(root / "state.json", state)
                    (peers / f"{site}.running").unlink(missing_ok=True)
                    del active[site]
                # Wcześniej zakończone serwisy odzyskują możliwość pracy w tle,
                # gdy każdy katalog otrzymał już pierwszy przydział procesu.
                if not waiting and active and not interrupted and args.command == "update" and not args.url and not args.limit and not getattr(args, "new_only", False):
                    for site in requested:
                        report = reports.get(site, {})
                        if site in early_finished and site not in continued and site not in active and "after" in report and not report.get("stopped_on_errors"):
                            continued.add(site)
                            waiting.append(site)
                            (peers / f"{site}.running").touch()
            except KeyboardInterrupt:
                interrupted = True
                waiting.clear()
                stop_file.write_text("stop", encoding="ascii")
        while not messages.empty():
            print(messages.get_nowait(), flush=True)
    return [reports[site] for site in requested if site in reports], interrupted
