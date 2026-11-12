# Rozszerzenie katalogów audiobooków

## Cel
Dołączyć Nexto, Publio, BookBeat, Woblink, Legimi i Wolne Lektury do istniejącego
aktualizatora. Zachować schematy i dane czterech obecnych baz. Nowe bazy używają
tego samego modelu książek, autorów, lektorów, wydawców, opinii i kolejki.

## Kolejność wdrożenia
1. [x] Wspólny opis serwisów i bezpieczne tworzenie wyłącznie nowych baz.
2. [x] Nexto: potwierdzenie zamknięcia dystrybucji; parser archiwum, zapis i próby na żywo. Wyłączone z aktualizacji, okładki 404.
3. [x] Publio: odkrywanie, parser, okładki, zapis i próby na żywo.
4. [x] BookBeat: katalog rynku PL, odkrywanie, parser i próby na żywo.
5. [x] Woblink: odkrywanie, parser i próby na żywo.
6. [x] Legimi: oddzielenie audio od ebooków oraz TTS, odkrywanie i próby na żywo.
7. [x] Wolne Lektury: integracja publicznego API.
8. [x] GUI i CLI: wspólne tryby, limity, zatrzymanie, wznowienie i równoległość.
9. [x] Testy regresji dawnych serwisów i izolowane testy wszystkich nowych baz.
10. [x] Raport wdrożenia, instrukcja i jawne ograniczenia potwierdzone testami.

## Warunki włączenia serwisu
- Potwierdzony format audiobooka, stabilna tożsamość produktu i prawidłowa paginacja.
- Rozpoznane tytuł, autorzy, lektorzy, wydawca, opis, czas, język, ISBN i seria,
  o ile źródło udostępnia te dane; brak nie jest uzupełniany domysłem.
- Okładka zweryfikowana po pobraniu, bez sztucznego powiększania.
- Ponowne uruchomienie nie dodaje tej samej publikacji drugi raz.
- Błędy i przerwania pozostawiają zadania do ponowienia.
- Testy sieciowe zapisują do odrębnych baz diagnostycznych.
- Blokada lub brak dostępu do źródła jest raportowany, nie ukrywany jako sukces.

## Aktualny etap
Wdrożone pięć aktywnych źródeł i opcjonalny parser archiwum Nexto.
Nowe bazy zawierają po trzy zweryfikowane rekordy startowe. Pełne pobieranie
katalogów odbywa się podczas zwykłej aktualizacji, z trwałą kolejką i punktami wznowienia.
Szczegóły oraz wyniki: reports\new_sources\WDROZENIE.md.
