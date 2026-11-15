# Audyt kategorii, tagów i relacji — 2026-11-15

Sprawdzono 11 lokalnych baz. Dla 10 aktywnych serwisów pobrano po trzy aktualne karty produktów (30 przykładów), dodatkowo przeanalizowano strony z brakującymi kategoriami, odczyty Playwright i publiczne zapytania BookBeat. Nexto sprawdzono na trzech zapisanych próbkach archiwalnych; serwis jest wyłączony w konfiguracji aktywnego pobierania.

## Wykryte i poprawione błędy

- Empik już odczytywał tagi, także ukryte za „Więcej”, lecz nie był obsługiwany przez `--tags-only`. Dodano pobieranie przez Playwright i ponowne sprawdzanie wszystkich rekordów.
- Woblink publikował tagi własnego produktu, których parser nie zbierał. Są odczytywane wyłącznie z `#product-card-tags`, również etykiety promocyjne. Linki `/tag/` z menu są pomijane.
- LC rozcinało pojedynczą kategorię `kryminał, sensacja, thriller` na trzy etykiety. Obecnie zachowuje pełną nazwę, wyłącznie z właściwej sekcji `.book__desc`.
- Kategorie z JSON-LD mogły zastępować listę odczytaną z własnej karty. Zabezpieczono wybór obiektu przez ID/adres i zgodność tytułu, a listy własnych kategorii są zachowywane.
- W kodzie Woblinka lista breadcrumb mogła zastąpić wcześniej odczytany gatunek JSON-LD. Gatunki własnego produktu mają teraz pierwszeństwo; breadcrumb pozostaje w metadanych pomocniczych i służy jako fallback, gdy gatunku brak.
- Audioteka i Storytel zachowywały pojedynczą lub sklejoną nazwę zamiast listy linków kategorii. Parsery zachowują wszystkie własne kategorie.
- Audyt nie wykrywał brakujących kategorii. Dodano tę kontrolę i możliwość naprawy istniejących kategorii przez `--sync-categories`.
- Zapis wyłącznie dodający etykiety pozostawiał stare błędne dane po ponownym pobraniu. Sprawdzone etykiety są zastępowane; eksport offline nie przywraca starych etykiet ani potwierdzonych pustych tagów.

## Zakres źródeł

| Serwis | Kategorie | Tagi / inne etykiety | Relacje rekomendacji |
|---|---|---|---|
| Audioteka | Własna tabela i gatunki JSON-LD | Nie potwierdzono osobnej sekcji tagów tematycznych | Ogólne „Polecane” nie jest traktowane jako podobieństwo książki |
| Storytel | Linki kategorii i własne dane strukturalne | Tagi książki | Sekcja „Książki podobne do…” |
| Empik | Kategorie produktu, bez ogólnych pozycji „Audiobooki” i „Audiobooki i Ebooki” | Wszystkie spany tagów, także ukryte | `SimilarProducts`, bez pozostałych karuzel |
| LubimyCzytać | Pełna nazwa własnej kategorii, przecinki pozostają | Tagi książki | „Czytelnicy … przeczytali również”; linki do opinii zachowane bez zgadywania ID książki |
| Publio | Wszystkie kategorie z tabeli własnej publikacji | Nie potwierdzono osobnej listy tagów w próbkach | Istnieje dynamiczny kontener Luigi’s Box; nie potwierdzono jego listy wyników. Próba renderowania zwróciła 403 |
| BookBeat | Pełna lista gatunków własnej książki | `content_type` oraz istniejące etykiety ocen książki i narracji | Publiczna lista „Inni też lubią”, zweryfikowana przez ID książki |
| Woblink | Gatunki produktu, breadcrumb wyłącznie jako fallback | Tagi własnego produktu | Sekcja „Podobne do…” |
| Legimi | `primaryCategory` i `additionalCategories` własnej książki | Nie potwierdzono osobnych tagów w próbkach | Sekcja „Podobne”, z informacją o dostępności audio z ikon formatów |
| Wolne Lektury | Gatunki z API | Epoki i rodzaje literackie jako oddzielne rodzaje etykiet | `parent` i `children` pozostają metadanymi struktury utworu, nie są udawanym podobieństwem |
| Virtualo | Kategorie z tabeli produktu | Nie potwierdzono osobnych tagów w próbkach | `products_top_view` to kontener listy top; nie przypisano go jako podobieństwa. Renderowanie próby zostało zablokowane |
| Nexto | Brak kategorii w trzech próbkach archiwalnych | Nie potwierdzono tagów | Brak potwierdzonej listy relacji |

Słowa kluczowe JSON-LD są dodatkowym fallbackiem tagów tylko dla zweryfikowanego własnego produktu. Ogólne metatagi SEO nie są traktowane jako tagi tematyczne. Brak danych nie jest uzupełniany przez odgadywanie kategorii z opisu lub tagów.

## Integralność i historyczne braki baz

Stan przed naprawą parserów. Liczba z kategorią oznacza poprawne powiązanie z niepustą nazwą, nie potwierdzenie semantyki każdej historycznej kategorii na stronie.

| Baza | Rekordy z kategorią / wszystkie |
|---|---:|
| Audioteka | 40 252 / 77 434 |
| Storytel | 61 637 / 61 637 |
| Empik | 30 576 / 30 624 |
| LC | 703 872 / 703 872 |
| Publio | 9 854 / 9 854 |
| BookBeat | 2 519 / 2 534 |
| Woblink | 7 093 / 10 438 |
| Legimi | 6 620 / 6 798 |
| Wolne Lektury | 1 565 / 1 565 |
| Virtualo | 15 767 / 15 767 |
| Nexto | 0 / 3 |

Nie stwierdzono osieroconych powiązań kategorii ani tagów w bazach źródłowych. W wybranych rekordach z brakami Audioteki, BookBeat i Legimi aktualna karta rzeczywiście nie udostępniała kategorii.

Wykonano eksport offline wszystkich 30 624 rekordów Empiku: 30 576 rekordów z sygnałami, 17 844 z tagami, bez błędów i bez pobierania stron. Historyczne tagi w `data\empik.db` były już obecne dla 17 840 rekordów; dodatkowe dane pochodziły z archiwum wcześniejszych odczytów.

## Kontrakt relacji i kompletności

- `similar_titles` zachowuje dotychczasowy schemat i zawiera tylko adresy z poprawnym ID produktu.
- `related_links` dodaje rodzaj `similar`, `read_also` lub `also_liked`, dokładny adres, opcjonalny `target_key`, kolejność, tytuł, nagłówek i `audio_available`.
- Link LC do opinii ma `target_key = NULL`. ID opinii ani liczba z adresu obrazka nie stają się ID książki.
- Legimi i BookBeat mogą polecać również ebooki. `audio_available = 0` oznacza wyraźne dane o braku audio, `NULL` — brak rozstrzygającej informacji. Do rekomendacji audiobooków należy stosować własny katalog i te flagi.
- BookBeat: sprawdzono trzy książki, każda zwróciła pierwszą partię 25 pozycji. Kolejna partia jest jawnie wskazana w `recommendation_coverage.next_url`; `complete = 0`.
- HTML: `scope = 'page_section'`, `complete = NULL`. Zebrano elementy rzeczywiście obecne w sekcji; nie zadeklarowano kompletności całego algorytmu rekomendacji.
- Błąd publicznego API nie zamienia się w pomyślny odczyt pustej listy ani nie usuwa zapisanych wcześniej relacji.

## Uzupełnienie wszystkich istniejących rekordów

```powershell
python main.py backfill --tags-only --services empik --recheck --limit 0
python main.py backfill --recommendations --recheck --sync-categories --limit 0
```

Drugi tryb odczytuje ponownie wszystkie istniejące rekordy aktywnych serwisów, aktualizuje sygnały rekomendacji i synchronizuje tylko kategorie w bazach źródłowych. Nie przepisuje tytułów, autorów ani okładek. Brak kategorii w źródle pozostawia dotychczasową wartość. Każdy tryb ma trwały punkt wznowienia. Masowego odczytu całego historycznego katalogu przez sieć nie uruchomiono w ramach audytu.

Testy obejmują schematy wszystkich serwisów, 15 fragmentów aktualnych sekcji rekomendacji, własność danych strukturalnych, dokładne kategorie, ukryte tagi, odświeżanie etykiet, błędy API, wznowienie, migrację bazy rekomendacji i zmianę wyłącznie kategorii w istniejących rekordach. Pełne lokalne HTML i raporty są w `reports\tag_category_audit` i nie są publikowane w repozytorium.

Do `recommendations.db` zapisano także 30 aktualnie zweryfikowanych próbek i 357 ich relacji. Przed tym zapisem utworzono kopię bazy rekomendacji. Bazy źródłowe nie były w tym kroku zmieniane. Końcowy pełny zestaw: 198 testów, wynik OK.


## Dodatkowa kontrola przypisania danych

Po audycie dodano domyślny tryb GUI „Pełne uzupełnienie i weryfikacja danych”.
Niepewne dane są odrzucane przed zapisem: weryfikowane są ID w odpowiedzi,
spójność canonical/OG, tytuł właściwego nagłówka, aliasy i docelowy rekord SQLite.
Wydanie audio z JSON-LD nie może dostarczać metadanych innej książki.
Karta podobnego produktu Empiku musi mieć ID zgodne z linkiem, a sekcja wskazująca
tytuł źródłowy musi dotyczyć otwartej książki. Raport i GUI pokazują odrzucone dane.

Przeprowadzono ponowną kontrolę 30 zapisanych aktualnych kart oraz testy
na zapisanych próbkach wszystkich serwisów. Pełna paginacja publicznej listy
BookBeat została również sprawdzona na trzech rzeczywistych książkach:
po 30 relacji w dwóch partiach, `complete = true`, bez następnej strony.
Tryb pełny nie miesza świeżych danych z historycznymi etykietami archiwum.
Ograniczenia dostępności dynamicznych sekcji Publio i Virtualo pozostają aktualne;
nie są zastępowane odgadywanymi listami podobieństw.

Dodatkowo usunięto pobieranie autorów z obcych obiektów JSON-LD LC,
wydawców z dowolnych części strony i kategorii z breadcrumbów innego produktu.
Archiwum jest sprawdzane względem klucza książki przed użyciem jego danych.
Nieprawidłowe historyczne adresy są raportowane zamiast cichego pomijania.
Potwierdzone aliasy są zapisywane pod kanonicznym ID; rozbieżność tytułu w bazie
z tytułem zweryfikowanego odczytu wymaga sprawdzenia i blokuje przypisanie danych.
Końcowa walidacja tej aktualizacji: pełny zestaw 221 testów oraz 24 testy
modułu tożsamości i GUI — wynik OK. Produkcyjnego pełnego pobierania nie uruchamiano.
