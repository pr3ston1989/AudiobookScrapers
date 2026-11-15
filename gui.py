"""Desktop launcher with streamed progress and graceful worker interruption."""

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

ROOT = Path(__file__).resolve().parent
SERVICES = {"audioteka": "Audioteka", "storytel": "Storytel", "empik": "Empik", "lubimyczytac": "LubimyCzytać"}
from sources import SOURCES, TAG_SITES
SERVICES.update({site: source["label"] for site, source in SOURCES.items() if source["active"]})
FULL_MODE = "Pełne uzupełnienie i weryfikacja danych"
MODES = {FULL_MODE: ["backfill", "--complete"], "Aktualizuj wszystkie bazy": [], "Szybkie sprawdzenie nowości": ["--fast"],
         "Wznów kolejkę": ["--resume"], "Przegląd katalogów i uzupełnianie braków": ["--full-catalog"],
         "Sprawdź parsowanie (bez zapisu)": ["diagnose", "--samples", "3"], "Statystyki baz": ["stats"],
         "Audyt brakujących metadanych": ["audit"]}
MODES["Tylko nowości (bez napraw starych rekordów)"] = ["--new-only", "--fast"]
MODES["Napraw miniaturki Audioteki"] = ["repair-covers"]
MODES["Uzupełnij wszystkie braki w bazach"] = ["backfill"]
MODES["Uzupełnij tylko tagi LubimyCzytać, Storytela, Empiku i Woblinka"] = ["backfill", "--tags-only"]
MODES["Sprawdź ponownie wszystkie tagi Empiku"] = ["backfill", "--tags-only", "--recheck"]
MODES["Zbierz dane do rekomendacji"] = ["backfill", "--recommendations"]
MODES["Sprawdź ponownie dane do rekomendacji"] = ["backfill", "--recommendations", "--recheck"]
MODES["Sprawdź kategorie i rekomendacje we wszystkich rekordach"] = ["backfill", "--recommendations", "--recheck", "--sync-categories"]
MODES["Eksportuj dane do rekomendacji bez pobierania"] = ["backfill", "--recommendations", "--local-only"]


class ScraperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Scrapery audiobooków — aktualizacja baz")
        icon = ROOT / "assets" / "scrapery-audiobookow.ico"
        if icon.is_file():
            try:
                self.root.iconbitmap(str(icon))
            except tk.TclError:
                pass
        self.root.geometry("1180x900")
        self.root.minsize(850, 620)
        self.process = None
        self.stop_file = None
        self.messages = queue.Queue()
        self.report = None
        self.closing = False
        self.service_errors = {}
        self.selected = {site: tk.BooleanVar(value=SOURCES.get(site, {}).get("active", True)) for site in SERVICES}
        self.mode = tk.StringVar(value=next(iter(MODES)))
        self.limit = tk.StringVar(value="0")
        self.refresh = tk.StringVar(value="50")
        self.visible = tk.BooleanVar(value=True)
        self.backup = tk.BooleanVar(value=False)
        self.parallel = tk.BooleanVar(value=True)
        self.workers = tk.StringVar(value="4")
        self.interval = tk.StringVar(value="0")
        self.url = tk.StringVar()
        self.status = tk.StringVar(value="Gotowy. Bazy są aktualizowane w folderze data.")
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Aktualizacja kolekcji", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Wybierz serwisy i kliknij Uruchom. Postęp i niedokończone zadania są zapisywane.").pack(anchor="w", pady=(2, 12))
        services = ttk.Frame(frame)
        services.pack(fill="x")
        for index, (site, label) in enumerate(SERVICES.items()):
            ttk.Checkbutton(services, text=label, variable=self.selected[site], state="normal" if SOURCES.get(site, {}).get("active", True) else "disabled").grid(row=index // 5, column=index % 5, sticky="w", padx=(0, 20), pady=3)
        options = ttk.LabelFrame(frame, text="Uruchomienie", padding=10)
        options.pack(fill="x", pady=12)
        ttk.Combobox(options, textvariable=self.mode, values=list(MODES), state="readonly", width=48).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(options, text="Pokaż przeglądarkę Empiku", variable=self.visible).grid(row=0, column=2, columnspan=2, sticky="w", padx=20)
        for column, label, variable in ((0, "Limit rekordów / serwis (0 = wszystkie)", self.limit),
                                        (2, "Odśwież istniejące / serwis", self.refresh)):
            ttk.Label(options, text=label).grid(row=1, column=column, sticky="w", pady=(10, 0))
            ttk.Entry(options, textvariable=variable, width=10).grid(row=1, column=column + 1, sticky="w", padx=8, pady=(10, 0))
        ttk.Label(options, text="Powtarzaj co godzin (0 = jeden przebieg)").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(options, textvariable=self.interval, width=10).grid(row=2, column=1, sticky="w", padx=8, pady=(8, 0))
        ttk.Label(options, text="Opcjonalnie: adres jednej książki").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(options, textvariable=self.url).grid(row=3, column=1, columnspan=3, sticky="ew", padx=8, pady=(8, 0))
        options.columnconfigure(3, weight=1)
        ttk.Checkbutton(options, text="Aktualizuj serwisy równolegle", variable=self.parallel).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(options, text="Serwisy jednocześnie").grid(row=4, column=2, sticky="w", pady=(8, 0))
        ttk.Combobox(options, textvariable=self.workers, values=list(range(1, len(SERVICES) + 1)), state="readonly", width=5).grid(row=4, column=3, sticky="w", padx=8, pady=(8, 0))
        ttk.Checkbutton(options, text="Zrób kopię baz przed uruchomieniem (opcjonalnie)", variable=self.backup).grid(row=5, column=0, columnspan=4, sticky='w', pady=(8, 0))
        self.mode_description = tk.StringVar()
        ttk.Label(options, textvariable=self.mode_description, wraplength=1000).grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.mode.trace_add("write", lambda *_: self.describe_mode())
        self.describe_mode()
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(0, 12))
        self.run_button = ttk.Button(buttons, text="Uruchom", command=self.start)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Zatrzymaj bezpiecznie", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="Raporty", command=lambda: self.open_path(ROOT / "reports")).pack(side="right")
        ttk.Button(buttons, text="Bazy", command=lambda: self.open_path(ROOT / "data")).pack(side="right", padx=8)
        ttk.Button(buttons, text="Instrukcja", command=lambda: self.open_path(ROOT / "README.md")).pack(side="right")
        self.table = ttk.Treeview(frame, columns=("books", "new", "updated", "missing", "state"), show="tree headings", height=len(SERVICES))
        for key, label, width in (("#0", "Serwis", 130), ("books", "Książki w bazie", 110), ("new", "Dodano", 70),
                                  ("updated", "Odświeżono", 90), ("missing", "Błędy / niepełne", 120), ("state", "Stan", 330)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, stretch=key == "state")
        for site, label in SERVICES.items():
            self.table.insert("", "end", iid=site, text=label, values=("—", 0, 0, 0, "Gotowy"))
        self.table.pack(fill="x")
        ttk.Label(frame, textvariable=self.status, wraplength=1000).pack(anchor="w", pady=8)
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 8))
        self.logs = ScrolledText(frame, height=12, font=("Consolas", 9), state="disabled")
        self.logs.pack(fill="both", expand=True)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)
        self.load_counts()

    def describe_mode(self):
        """Explain the scope and rejection policy without exposing internal command arguments."""
        self.mode_description.set(
            "Sprawdza istniejące rekordy wybranych serwisów, uzupełnia braki, kategorie, tagi i rekomendacje. "
            "Limit 0 obejmuje całą bazę. Niepotwierdzone lub sprzeczne dane nie zostaną zapisane; "
            "wynik pokaże odrzucone rekordy i niepełne odczyty."
            if self.mode.get() == FULL_MODE else
            "Zakres zależy od wybranego trybu. Postęp jest zapisywany i można go wznowić."
        )

    def load_counts(self):
        """Read every book count off the UI thread without opening the scraper."""
        messages = self.messages
        def read():
            from contextlib import closing
            try:
                config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
                for site in SERVICES:
                    relative = config.get('databases', {}).get(site)
                    if not relative:
                        continue
                    path = (ROOT / relative).resolve()
                    try:
                        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as conn:
                            count = conn.execute('SELECT COUNT(*) FROM books').fetchone()[0]
                        messages.put({'event': 'inventory', 'site': site, 'books': count})
                    except (OSError, sqlite3.Error) as exc:
                        messages.put(f'Nie można odczytać licznika {site}: {exc}')
            except (OSError, ValueError) as exc:
                messages.put(f'Nie można odczytać konfiguracji liczników: {exc}')
        threading.Thread(target=read, daemon=True).start()

    def command(self):
        """Build an argument list without shell interpolation."""
        services = [site for site, selected in self.selected.items() if selected.get()]
        if not services and not self.url.get().strip() and self.mode.get() not in ("Napraw miniaturki Audioteki", "Sprawdź ponownie wszystkie tagi Empiku"):
            raise ValueError("Wybierz co najmniej jeden serwis.")
        limit, refresh = int(self.limit.get()), int(self.refresh.get())
        interval = float(self.interval.get().replace(",", "."))
        if min(limit, refresh, interval) < 0:
            raise ValueError("Limity i odstęp nie mogą być ujemne.")
        workers = int(self.workers.get()) if self.parallel.get() else 1
        if not 1 <= workers <= len(SERVICES):
            raise ValueError(f"Liczba serwisów jednocześnie musi wynosić 1–{len(SERVICES)}.")
        mode = MODES[self.mode.get()]
        if "repair-covers" in mode:
            services = ["audioteka"]
            if self.url.get().strip():
                raise ValueError("Usuń adres pojedynczej książki przed naprawą okładek.")
        if self.mode.get() == "Sprawdź ponownie wszystkie tagi Empiku":
            services = ["empik"]
            if self.url.get().strip():
                raise ValueError("Usuń adres pojedynczej książki przed sprawdzeniem wszystkich tagów Empiku.")
        if "--tags-only" in mode:
            services = [site for site in services if site in TAG_SITES]
            if not services and not self.url.get().strip():
                raise ValueError("Wybierz LubimyCzytać, Storytel, Empik lub Woblink.")
        if interval and any(value in mode for value in ("diagnose", "stats", "audit", "repair-covers", "backfill")):
            raise ValueError("Powtarzanie jest dostępne dla aktualizacji.")
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        command = [str(executable), "-u", str(ROOT / "main.py"), *mode]
        if services:
            command += ["--services", *services]
        command += ["--limit", str(limit)]
        if "backfill" not in mode:
            command += ["--refresh-limit", "0" if "--resume" in mode and refresh == 50 else str(refresh)]
        command += ["--workers", str(workers)]
        command.append('--backup' if self.backup.get() else '--no-backup')
        if not self.visible.get():
            command.append("--headless")
        if interval:
            command += ["--interval-hours", str(interval)]
        if self.url.get().strip():
            command += ["--url", self.url.get().strip()]
        return command

    def start(self):
        if self.process is not None:
            return
        try:
            command = self.command()
            stops = ROOT / "state" / "stops"
            stops.mkdir(parents=True, exist_ok=True)
            self.stop_file = stops / f"{uuid.uuid4().hex}.request"
            env = dict(os.environ, NOKTURN_GUI_EVENTS="1", PYTHONIOENCODING="utf-8", NOKTURN_STOP_FILE=str(self.stop_file))
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", creationflags=flags)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Nie można uruchomić", str(exc), parent=self.root)
            return
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        if "backfill" in command:
            self.status.set("Uruchomiono uzupełnianie istniejących rekordów.")
        elif "repair-covers" in command:
            self.status.set("Uruchomiono naprawę rozdzielczości okładek.")
        else:
            self.status.set("Uruchomiono. Pierwszy przegląd katalogów może potrwać dłużej.")
        selected = [site for site in SERVICES if site in command]
        if self.url.get().strip():
            from urls import identity
            selected = [site for site in SERVICES if identity(site, self.url.get().strip())]
        self.event({"event": "schedule", "sites": selected,
                    "workers": min(int(command[command.index("--workers") + 1]), len(selected))})
        process = self.process
        def read_output():
            for line in process.stdout:
                self.messages.put(line.rstrip())
            process.stdout.close()
            self.messages.put(("exit", process.wait()))
        threading.Thread(target=read_output, daemon=True).start()

    def stop(self):
        if self.process and self.process.poll() is None:
            # Plik znacznika działa także dla procesu bez konsoli Windows.
            self.stop_file.write_text("stop", encoding="ascii")
            self.status.set("Zatrzymywanie: scraper zapisze postęp po bieżącym żądaniu.")
            self.stop_button.configure(state="disabled")

    def log(self, line):
        self.logs.configure(state="normal")
        self.logs.insert("end", line + "\n")
        if int(self.logs.index("end-1c").split(".")[0]) > 3000:
            self.logs.delete("1.0", "500.0")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def event(self, event):
        kind, site = event.get("event"), event.get("site")
        if kind == "schedule":
            self.service_errors = {}
            selected = event["sites"]
            workers = event["workers"]
            for service in SERVICES:
                values = list(self.table.item(service, "values"))
                values[1:4] = [0, 0, 0]
                values[4] = "Oczekuje na wolny proces" if service in selected else "Nie wybrano"
                self.table.item(service, values=values)
            self.status.set(f"Wybrano {len(selected)} serwisów; jednocześnie do {workers}; oczekuje {max(0, len(selected) - workers)}.")
            return
        if kind == "report":
            self.report = Path(event["path"])
        if site not in SERVICES:
            return
        values = list(self.table.item(site, "values"))
        if kind == 'inventory':
            values[0] = event['books']
        elif kind == "phase":
            values[4] = event["phase"]
            self.status.set(f"{SERVICES[site]}: {event['phase']}")
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        elif kind == "discovery":
            values[4] = f"Linki: {event['discovered']}; mapy: {event['maps']}; strony: {event['pages']}"
        elif kind == "progress":
            if 'books' in event:
                values[0] = event['books']
            values[1:4] = [event["inserted"], event["updated"], event["incomplete"]]
            values[4] = f"Przetwarzanie {event['done']} / {event['total']}"
            if event.get('skipped'):
                values[4] += f"; pominięto {event['skipped']}"
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(1, event["total"]), value=event["done"])
            self.status.set(f"{SERVICES[site]}: {event['url']}")
        elif kind == "summary":
            if event.get('errors'):
                errors = getattr(self, 'service_errors', {})
                errors[site] = event['errors'][-1]
                self.service_errors = errors
            stats = event.get("after", event.get("before", {}))
            values[0:4] = [stats.get("books", values[0]), event.get("inserted", 0), event.get("updated", 0), event.get("incomplete", 0) + len(event.get("errors", []))]
            coverage = event.get("coverage", {})
            if event.get("interrupted"):
                values[4] = "Przerwano; postęp zapisany — można wznowić"
                if event.get("errors"):
                    values[4] += f"; błędy: {len(event['errors'])} (raport)"
            elif event.get("errors"):
                if event.get("identity_rejections"):
                    values[4] = f"Nie zapisano niepotwierdzonych danych: {len(event['identity_rejections'])}; sprawdź raport"
                    if event.get("stopped_on_identity_conflicts"):
                        values[4] += "; wstrzymano serwis"
                elif event.get('stopped_on_access_limit'):
                    values[4] = 'Serwis odmawia dostępu (HTTP 403); kolejka zapisana'
                elif event.get("stopped_on_rate_limit"):
                    values[4] = "Serwis ogranicza żądania (HTTP 429); kolejka zapisana"
                elif event.get("stopped_on_errors"):
                    values[4] = "Zatrzymano po 5 kolejnych błędach; sprawdź raport"
                elif "after" in event:
                    values[4] = f"Przebieg zakończony; błędy: {len(event['errors'])} (szczegóły w raporcie)"
                else:
                    values[4] = f"Błąd: {event['errors'][-1]}"
            elif event.get("unresolved_signals"):
                values[4] = f"Niepełne rekomendacje: {len(event['unresolved_signals'])}; sprawdź raport"
            elif event.get("complete_mode") and event.get("metadata_gaps", {}).get("incomplete_books"):
                values[4] = f"Sprawdzono {event['attempted']}; nadal braki danych: {event['metadata_gaps']['incomplete_books']}"
            elif "attempted" in event:
                values[4] = (f"Sprawdzono {event['attempted']}; pobrano {event['downloaded']}; "
                             f"z danymi: {event['signals']}; z tagami: {event['tagged']}")
            elif event.get("incomplete"):
                values[4] = f"Zapisano dane; braki: {event['incomplete']} (ponowimy później)"
            elif event.get('deferred_errors'):
                values[4] = f"Zapisano dane; odłożono {len(event['deferred_errors'])} wadliwych kart"
            elif coverage.get("catalog_pages_pending"):
                values[4] = f"Katalog w toku: {coverage['catalog_pages_pending']} stron do wznowienia"
            elif coverage.get("unresolved"):
                values[4] = f"Pozostało {coverage['unresolved']} rekordów do uzupełnienia"
            elif event.get("metadata_gaps", {}).get("incomplete_books"):
                values[4] = f"Braki metadanych: {event['metadata_gaps']['incomplete_books']} rekordów"
            else:
                values[4] = "Katalog aktualny; brak nowych książek" if event.get('catalog_complete') and not event.get('new_candidates') else "Przebieg zakończony"
        self.table.item(site, values=values)

    def poll(self):
        try:
            while True:
                message = self.messages.get_nowait()
                if isinstance(message, dict):
                    self.event(message)
                elif isinstance(message, tuple):
                    code = message[1]
                    self.process = None
                    if self.stop_file:
                        self.stop_file.unlink(missing_ok=True)
                        self.stop_file = None
                    self.run_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.progress.stop()
                    for site in SERVICES:
                        values = list(self.table.item(site, "values"))
                        if values[4] in ("Oczekuje na wolny proces", "Uruchamianie"):
                            values[4] = "Nie uruchomiono; przebieg zatrzymany" if code == 130 else "Nie uruchomiono; sprawdź raport"
                            self.table.item(site, values=values)
                    failed = ', '.join(SERVICES[site] for site in self.service_errors)
                    self.status.set("Zakończono. Stan i ewentualne braki są w tabeli." if code == 0 else "Zatrzymano; kolejka zostaje do wznowienia." if code == 130 else f"Zakończono. Uwagi dla: {failed or 'uruchomienia'}. Przyczyna jest w tabeli i dzienniku.")
                    if self.closing:
                        self.root.destroy()
                        return
                elif message.startswith("@@NOKTURN "):
                    try:
                        self.event(json.loads(message[10:]))
                    except (ValueError, KeyError, tk.TclError) as exc:
                        self.log(f"Błąd odczytu postępu: {exc}")
                else:
                    self.log(message)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def open_path(self, path):
        if path.exists():
            os.startfile(path)
        else:
            messagebox.showinfo("Brak pliku", "Folder pojawi się po pierwszym uruchomieniu.", parent=self.root)

    def close(self):
        if self.process is not None:
            self.closing = True
            self.stop()
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    ttk.Style(root).theme_use("vista" if os.name == "nt" else "clam")
    ScraperGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
