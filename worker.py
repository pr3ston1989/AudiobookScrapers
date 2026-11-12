"""Internal worker entry point; launched only by the locked parent process."""

import json
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

import main
from events import emit, check_stop
from background import other_workers, plan, merge


def execute(job):
    """Persist a service checkpoint even after cooperative interruption."""
    site = job["site"]
    started = time.time()
    deadline = job.get("cycle_started", started)
    main.ROOT = Path(job["root"])
    settings = job["settings"]
    settings["databases"] = {key: Path(value) for key, value in settings["databases"].items()}
    args = SimpleNamespace(**job["args"])
    state = job["state"]
    state_path = main.ROOT / "state" / site / "run.json"
    if state_path.exists():
        state = main.read_state(state_path, state)
    interrupted = False
    report = job.get("previous_report")
    previous_elapsed = report.get("elapsed_seconds", 0) if report else 0
    args.attempted_tasks = set(job.get("attempted_tasks", []))
    sweep = {"attempted_tasks": args.attempted_tasks}
    unlimited = args.command == 'update' and not args.url and not args.limit and not getattr(args, 'new_only', False)
    ready = False
    try:
        check_stop()
        if report is None:
            original_limit = args.limit
            if unlimited:
                args.limit = settings.get('work_batch_size', 100)
            try:
                report = main.run_site(site, settings, args, state)
            finally:
                args.limit = original_limit
        args.partial_report = None
        if args.command == "update":
            main.write_json(state_path, state)
        ready = not job.get('peers') or (Path(job['peers']) / 'ready').exists()
        while not report.get("stopped_on_errors") and "after" in report and (other_workers(job) or unlimited and ready):
            check_stop()
            batch = plan(site, settings, args, deadline, sweep)
            if batch is None:
                break
            batch.partial_report = None
            batch.totals = {field: report.get(field, 0) for field in ("inserted", "updated", "incomplete")}
            batch_settings = dict(settings, background_retry_cutoff=deadline,
                                  catalog_page_budget=min(settings["catalog_page_budget"], 5),
                                  lc_id_batch=min(settings["lc_id_batch"], 25))
            batch.limit = settings.get('work_batch_size', 100)
            emit("phase", site=site, phase="Dalsze pobieranie i przegląd katalogu")
            logging.info("[%s] Inne serwisy nadal pracują; rozpoczynam kolejną paczkę uzupełnień", site)
            report = merge(report, main.run_site(site, batch_settings, batch, state))
            batch.partial_report = None
            main.write_json(state_path, state)
    except KeyboardInterrupt:
        interrupted = True
        partial = getattr(batch if "batch" in locals() else args, "partial_report", None)
        if partial is not None and partial is not report:
            report = merge(report, partial) if report else partial
        report = report or {"site": site, "errors": [], "warnings": []}
        report["warnings"].append("Przerwano; postęp i kolejka zostały zapisane")
        report.update(interrupted=True, complete=False, queue_complete=False)
    except Exception as exc:
        logging.exception("[%s] Błąd aktualizacji", site)
        partial = getattr(batch if "batch" in locals() else args, "partial_report", None)
        if partial is not None and partial is not report:
            report = merge(report, partial) if report else partial
        report = report or {"site": site, "errors": [], "warnings": []}
        report["errors"].append(str(exc))
        report.update(complete=False, queue_complete=False)
    finally:
        if args.command == "update":
            main.write_json(state_path, state)
        if job.get("peers"):
            (Path(job["peers"]) / f"{site}.running").unlink(missing_ok=True)
    report.update(worker_pid=os.getpid(), started_at=report.get("started_at", started), finished_at=time.time(),
                  elapsed_seconds=round(previous_elapsed + time.time() - started, 3))
    emit("summary", **report)
    main.write_json(Path(job["result"]), {"report": report, "state": state, "interrupted": interrupted,
                    "attempted_tasks": list(args.attempted_tasks),
                    "initial_pass_only": bool(job.get("peers") and not ready)})
    return 130 if interrupted else int(bool(report["errors"]))


def entry():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    logs = Path(job["root"]) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(logs / f"{job['site']}.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ])
    if hasattr(signal, "SIGBREAK"):
        def stop_on_break(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, stop_on_break)
    return execute(job)


if __name__ == "__main__":
    raise SystemExit(entry())
