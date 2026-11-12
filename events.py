"""Optional machine-readable progress for the desktop interface."""

import json
import os
from pathlib import Path

STOP_FILE = Path(os.environ.get("NOKTURN_STOP_FILE", Path(__file__).resolve().parent / "stop.request"))


def check_stop():
    """Interrupt only at safe boundaries, leaving SQLite transactions recoverable."""
    if STOP_FILE.exists():
        raise KeyboardInterrupt


def emit(kind, **values):
    if os.environ.get("NOKTURN_GUI_EVENTS") == "1":
        print("@@NOKTURN " + json.dumps({"event": kind, **values}, ensure_ascii=False), flush=True)
