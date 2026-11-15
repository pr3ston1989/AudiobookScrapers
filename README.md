# Nokturn — główny scraper

Uruchom `start.bat`, zostaw zaznaczone aktywne serwisy i kliknij **Uruchom**.
Otwiera się GUI; Empik uruchamia widoczną przeglądarkę. Instalacja w obecnym
środowisku nie jest potrzebna. Na nowym komputerze najpierw `install.bat`.
Python 3.10+; GUI korzysta z wbudowanego tkinter.

## Nowe źródła — 15 października 2026

Publio, BookBeat, Woblink, Legimi, Wolne Lektury i Virtualo są włączone w GUI oraz
w domyślnym uruchomieniu CLI. Razem z dotychczasowymi źródłami daje to dziesięć
aktywnych serwisów. Domyślnie pracują cztery niezależne procesy; pole
**Serwisy jednocześnie** pozwala wybrać od jednego do dziesięciu.
Przykład: wybrano sześć serwisów i ustawiono cztery procesy — dwa źródła
mają status **Oczekuje na wolny proces** i ruszą po zwolnieniu miejsca.
Ustawienie sześciu pozwala uruchomić wszystkie sześć jednocześnie.
Wcześniej zakończone serwisy mogą wrócić do uzupełniania w tle po przydzieleniu
procesu każdemu wybranemu serwisowi. Zatrzymanie i wznowienie obejmuje nowe źródła.

Nowe bazy znajdują się w `data\publio.db`, `data\bookbeat.db`, `data\woblink.db`,
`data\legimi.db`, `data\wolnelektury.db` i `data\virtualo.db`. Przy wdrożeniu
uzupełniono je zweryfikowanymi rekordami startowymi z lokalnymi okładkami.
**Pełne katalogi nie zostały jeszcze pobrane.**
Pierwsza zwykła aktualizacja wykryje książki i rozpocznie ich pobieranie.
Kolejka i przegląd katalogów są zapisywane w `state\<serwis>`; kolejne uruchomienia
kontynuują pracę. Dodatkowe pola, m.in. ISBN i identyfikatory wydania audio,
pozostają w `state\<serwis>\coverage.db`, bez zmiany schematów głównych baz.

BookBeat korzysta z map produktów rynku PL, ponieważ parametr strony kategorii
powtarzał tę samą zawartość. Mapy obejmują także ebooki: parser odrzuca produkty
bez wydania audio i odkłada ich ponowną kontrolę o siedem dni. Legimi korzysta
z katalogu filtrowanego do audiobooków, wybiera dane wydania audio i odrzuca TTS.
Pełny katalog Legimi wymaga także `sort=latest`; samo `filters=audiobooks`
zwraca polecane kolekcje. Parser wymaga rzeczywistej paginacji i nie uznaje
takiej strony kolekcji za ukończony katalog. Po tej poprawce dawny punkt
zakończenia jest automatycznie unieważniany bez usuwania książek.
Publio oraz Woblink mają obsługę rzeczywistej paginacji. Wolne Lektury udostępniają
listę nagrań w publicznym API; opis w tej integracji jest fragmentem utworu.

Virtualo korzysta z kategorii audiobooków i ich paginacji. Sprawdzone mapy witryny
zawierają ebooki, więc nie służą do oznaczania katalogu audio jako kompletnego.
Nowości są przeglądane bez zatrzymania po znanych stronach, ponieważ widok
jest sortowany sprzedażą, a próby sort_id=4 zwracały HTTP 500. Poprawny widok
to `/audiobooki/nowosci/?list-grid=1`. Wstępna kolejka map jest automatycznie
wymieniana na kategorie audio; wykryte produkty i zadania pozostają zachowane.
Obsługiwane są również czasy z dniami, np. `1 dzień 7 godz. 28 min.` = 1888 minut.
Niejednoznaczny czas bez jednostek pozostaje do sprawdzenia; oryginalna wartość
jest zachowana w metadanych. Szczegóły: `reports\virtualo\WDROZENIE.md`.

Pobierane są największe potwierdzone obrazy udostępnione przez źródło.
W próbach: Publio do 800 px, BookBeat 2048×2048, Woblink 570×570,
Legimi do 512 px, Wolne Lektury 360×509, Virtualo 1400×1400 i 1200×1481.
Obrazy nie są sztucznie powiększane.
Obsługa oryginalnych okładek Audioteki pozostaje zachowana.

**Nexto zakończyło dystrybucję ebooków i audiobooków 30 czerwca 2026.**
Źródło jest wyłączone z domyślnych aktualizacji i nie pojawia się w GUI.
`data\nexto.db` zawiera trzy archiwalne rekordy, bez dostępnych okładek i czasu.
Parser pojedynczych historycznych adresów pozostał dostępny w CLI, ale Nexto
nie jest źródłem nowości. [Oficjalny komunikat](https://www.nexto.pl/informacja_o_zakonczeniu_dystrybucji.xml).

Pełny katalog oznacza zakończenie sprawdzonych ścieżek odkrywania; żaden scraper
nie może zagwarantować dostępności wszystkich publikacji po stronie serwisu.
Błędy, brakujące dane i niedokończone strony pozostają widoczne w raportach.
Wyniki testów i zakres zmian: `reports\new_sources\WDROZENIE.md`.

## Audyt danych dynamicznych — 15 października 2026

Szczegółowe wyniki: `reports\full_source_audit\WDROZENIE.md`.
Sprawdzono 30 rzeczywistych kart i przejścia katalogów dziesięciu aktywnych źródeł.
Empik korzysta z przeglądarki, czeka na dane i zamyka także opóźnione okna zgody.
LC obsługuje kolejne strony katalogu przez publiczny POST. BookBeat, Audioteka
i Legimi udostępniają część metadanych w stanie aplikacji; parser odczytuje
również te dane zamiast ograniczać się do widocznego tekstu HTML.

Uzupełniono ISBN, tłumacza i publikację Audioteki, serię/tom i ocenę narracji
BookBeat, cykl i rok samodzielnego wydania audio Legimi oraz kategorie Woblink.
Pozostałe szczegóły z tabel są zachowywane w pomocniczych metadanych.
Nowe pola uzupełniają się podczas pobierania i odświeżania danej książki;
wdrożenie nie pobiera automatycznie ponownie wszystkich istniejących rekordów.

BookBeat doładowuje opinie osobnym publicznym API. Aktualizator pobiera najnowszą
partię, zachowuje istniejące opinie w bazie i zapisuje `review_coverage`
z informacją o dalszych stronach. **Nie jest to pełne archiwum wszystkich opinii.**
Pozostałe adaptery zapisują opinie obecne w pobranej karcie; nie deklarują
kompletności całej historii opinii. Ocena kompletności książki dotyczy wymaganych
metadanych katalogowych i pobrania okładki, a nie wyczerpania wszystkich opinii.

W audycie Publio zwróciło poprawne trzy karty przez HTTP, ale zablokowało świeżą
przeglądarkę stroną Cloudflare. Blokada jest rozpoznawana jako błąd dostępu.
Nie potwierdzono tam pełnego porównania danych po wykonaniu JavaScriptu.

## Bazy i okładki

Wszystkie aktywne bazy są w nowym folderze:

| Serwis | Plik | Książki po przeniesieniu |
|---|---|---:|
| Audioteka | `data\audioteka.db` | 73 207 |
| Storytel | `data\storytel.db` | 48 913 |
| Empik | `data\empik.db` | 28 505 |
| LubimyCzytać | `data\lubimyczytac.db` | 358 640 |

Migracja obejmuje dane WAL, kontrolę SQLite `quick_check`, porównanie dokładnych
schematów i liczby rekordów **każdej tabeli**. Schematy baz nie zmieniły się.
Oryginały są zachowane w `legacy_backup` jako kopie sprzed migracji.
Raport: `reports\database_migration.json`.

Okładki przeniesiono do `media\<serwis>`. Ścieżki w aktywnych bazach wskazują
nowe położenie; poprawiono również dawne wskazania Empiku `D:\empik.com`.
Raport: `reports\media_migration.json`. Po zmianie położenia całego folderu
trzeba przeliczyć absolutne ścieżki okładek. Stare scrapery nie korzystają już
z przeniesionych baz; dalszą pracę wykonuj przez nowy program.

W źródłowym folderze LC było tylko 19 lokalnych okładek mimo 358 596 wpisów w
tabeli. Brakujące pliki są pobierane podczas odświeżania danej książki, a istniejący
wpis okładki jest aktualizowany. To nie jest pobranie wszystkich okładek od razu.

Baza dotychczasowego serwisu nieistniejąca, pusta lub o innym schemacie jest błędem konfiguracji.
Nowe, zarejestrowane serwisy mogą automatycznie utworzyć własną bazę;
nie zastępują w ten sposób baz dotychczasowych czterech scraperów.
Kopie baz są domyślnie wyłączone. Zaznacz w GUI „Zrób kopię baz przed uruchomieniem”
albo podaj CLI `--backup`, aby zapisać spójną kopię w `backups\<serwis>.db`.
Postęp i kolejki nadal są zapisywane automatycznie. Bazy nie są scalane.

## Tryby GUI

- **Aktualizuj wszystkie bazy**: sitemap, nowości, wznowienie kolejki, naprawa
  niepełnych rekordów, 50 odświeżeń istniejących książek na serwis. Pierwszy i
  okresowy przegląd katalogów działa w krótkich porcjach przeplatanych pobieraniem
  książek. Przerwany przegląd jest wznawiany. Pełny przegląd wraca co 7 dni.
- **Szybkie sprawdzenie nowości**: pomija dodatkowy okresowy przegląd katalogów.
- **Wznów kolejkę**: bez ponownego wykrywania linków.
- **Przegląd katalogów i uzupełnianie braków**: przechodzi katalogi w porcjach,
  bez zatrzymywania po znanych stronach Empiku.
  Może trwać wiele godzin; przerwanie pozostawia trwały punkt wznowienia.
- **Sprawdź parsowanie**: trzy produkty na serwis, pełny raport pól, bez zapisu
  do baz książek. **Statystyki baz**: same liczby rekordów i stan kolejki.

Limit rekordów `0` oznacza wszystkie dostępne zadania w kolejce. Limit nie
ogranicza wykrywania linków. Odstęp `0` oznacza pojedynczy przebieg; np. `12`
powtarza aktualizację co 12 godzin po zakończeniu poprzedniej. GUI musi pozostać
uruchomione. Można też podać adres jednej książki.

**Zatrzymaj bezpiecznie** kończy pracę po bieżącym żądaniu; zapisane rekordy
pozostają, a kolejka i katalogi są kontynuowane przy następnym uruchomieniu.
Dwie instancje blokuje wspólny plik blokady. Błąd serwisu nie blokuje pozostałych.

## Wykrywanie i kompletność

Audioteka i Storytel odczytują aktualne indeksy sitemap z robots.txt oraz wszystkie
ich części, zamiast korzystać z listy zapisanej na sztywno. Audioteka dodatkowo
skanuje nowości i kategorie, Storytel stronę główną, kategorie i toplisty.
Empik zaczyna od 18 kategorii ze starego scrapera z sortowaniem `publishDesc`
i w pełnym przeglądzie odkrywa także nowe kategorie podlinkowane w katalogu.
Szybki skan zatrzymuje się po trzech stronach samych znanych produktów; pełny
przegląd sprawdza także starsze strony.

LC korzysta z sitemap nowości, listy nowości, katalogu i kategorii. Paginacja LC
wymaga POST do `/book/standardFilteredDataPageContent`; same odnośniki `href="#"`
nie prowadzą do kolejnych książek. Dodatkowo skanowana jest paczka do 250 ID
między najwyższym znanym ID a nowymi odkrytymi numerami, z pominięciem istniejących
książek i dokładnych adresów dawnych potwierdzonych 404.

`state\<serwis>\coverage.db` przechowuje odnalezione URL, daty modyfikacji, brakujące pola,
ponowienia i strony katalogu. **Istniejący rekord nie oznacza zweryfikowanego
rekordu**. Parser wymaga tytułu, autorów, opisu i okładki, a dla audiobooków
sklepów także wydawcy i czasu. Niepełny rekord jest zapisany częściowo, oznaczony
jako niepełny i pobierany ponownie po co najmniej godzinie; kolejne błędy wydłużają
odstęp do 7 dni. 404/410 trafia do osobnej kategorii, z ponowną próbą po 7 dniach.
Puste pola z nowej odpowiedzi nie kasują wcześniejszych danych.

Raport pokazuje oddzielnie: pozostałe strony katalogów, brakujące/niepełne rekordy,
rekordy odziedziczone i rzeczywiście zweryfikowane. Przejście wszystkich
**publicznie udostępnionych źródeł** nie dowodzi kompletności wewnętrznego katalogu
serwisu: sitemap Storytela może być opóźniona, LC obejmuje w niej krótkie okno
nowości, a niektóre tytuły nie mają publicznych linków. Nie raportujemy
nieudowodnionego „100% wszystkich książek”. Nie pobieramy automatycznie wszystkich
stron recenzji ani opinii dostępnych tylko po zalogowaniu.

Empik zawsze korzysta z Playwrighta: profil przeglądarki jest zachowywany,
okno cookies jest zamykane i sprawdzane, strona przewijana, sekcje rozwijane,
a parser czeka na szczegóły produktu. Blokada lub brak wymaganych danych jest
błędem, a nie udaną aktualizacją. Przeglądarkę można ukryć w GUI.

## CLI i testy

```powershell
python main.py                          # zwykła aktualizacja
python main.py --limit 10               # krótka próbka zapisu
python main.py --resume --refresh-limit 0
python main.py diagnose --samples 3
python main.py stats
python main.py --full-catalog --catalog-page-budget 1000000
python main.py --interval-hours 12
python main.py --services lubimyczytac --lc-id-start 5000000 --lc-id-end 5100000 --lc-id-batch 1000
python -m unittest discover -s tests -v
python verify_live.py                   # ponowny audyt publicznych stron
```

Historyczny skan ID jest opcjonalny, porcjowany i zapisuje kursor. Przy ponawianiu
podawaj ten sam zakres. Nie przywraca masowo milionów dawnych 404.
`--no-backup`, `--no-covers`, `--headless`, `--fast` są opcjonalne.
`start.bat` z argumentami uruchamia CLI, bez argumentów — GUI.

Raporty: `reports\*.json`; dziennik: `logs\scraper.log`. Kod 0 oznacza brak
błędów przebiegu, a nie pełne pokrycie katalogu; 1 oznacza błędy, 130 przerwanie.

Audyt 10.10.2026: 12 publicznych stron (po trzy na serwis), także oceny,
recenzje, serie/tomy LC i tagi Empiku; wszystkie wymagane pola poprawne.
Raport: `reports\live_field_audit.json`. Widoczny Empik zaakceptował cookies i
pobrał lektora, czas oraz datę: `reports\empik_visible_browser.json`.
Druga strona katalogu LC zwróciła 30 pozycji, 29 różnych od pierwszej.
Ograniczone próby katalogów: `reports\catalog_smoke.json`.
46 testów obejmuje oryginalne schematy, transakcje, WAL, idempotencję, kolejkę,
trwałe wznowienie katalogu, paginację AJAX, pola z 12 stron oraz widgety GUI.

## Pliki implementacji

`main.py`, `database.py`, `discovery.py`, `http_client.py`, `urls.py`, `parse_books.py`,
`parsers\*.py`; GUI: `gui.py`, `events.py`; kontrola pokrycia: `crawl_state.py`,
`quality.py`; konfiguracja i uruchomienie: `config.json`, `start.bat`, `install.bat`,
`requirements.txt`; migracja: `migrate_databases.py`, `migrate_media.py`;
weryfikacja: `verify_live.py`, `tests\test_scraper.py`, `tests\test_coverage.py`,
`tests\fixtures\*`; dokumentacja: `README.md`, `.gitignore`.


## Ulepszenia po audycie pełnych baz (10.10.2026)

Próbki parserów nie dowodzą kompletności całej bazy. Audyt SQL przed próbnymi
naprawami wykazał:

| Serwis | Rekordy z brakiem wymaganych metadanych | Wybrane braki |
|---|---:|---|
| Audioteka | 73 207 | wydawca: 73 207; autorzy: 3 801; opis: 387 |
| Storytel | 19 376 | autorzy: 11 053; czas: 19 321; wydawca: 10 956 |
| Empik | 1 173 | czas: 1 173; autorzy: 50; wydawca: 48 |
| LC | 44 | URL okładki: 44 |

Przy zwykłej aktualizacji automatycznie dodajemy do kolejki do 100 odziedziczonych
niepełnych rekordów na serwis (`repair_limit`), z trwałym kursorem.
**Audyt brakujących metadanych** tylko odczytuje bazy.

### Uzupełnianie istniejących rekordów

**Uzupełnij wszystkie braki w bazach** przechodzi po istniejących książkach
z brakami wykrywanymi w audycie oraz sprawdza tagi LubimyCzytać, Storytela, Empiku i Woblinka.
Przy pobraniu karty uzupełnia także puste pola opcjonalne, które mają wartość
w źródle. Nowe tagi trafiają do osobnej bazy rekomendacji.

| Tryb GUI | Argumenty `main.py` | Dane |
|---|---|---|
| Uzupełnij tylko tagi LubimyCzytać, Storytela, Empiku i Woblinka | `backfill --tags-only` | Tagi z zapisanych danych oraz z kart bez wcześniejszego wyniku |
| Sprawdź ponownie wszystkie tagi Empiku | `backfill --tags-only --services empik --recheck --limit 0` | Wszystkie istniejące rekordy Empiku, również wcześniej sprawdzone lub mające stare tagi |
| Zbierz dane do rekomendacji | `backfill --recommendations` | Dane z dotąd niesprawdzonych kart |
| Sprawdź ponownie dane do rekomendacji | `backfill --recommendations --recheck` | Ponowne pobranie kart, także wcześniej sprawdzonych, z własnym punktem wznowienia |
| Sprawdź kategorie i rekomendacje we wszystkich rekordach | `backfill --recommendations --recheck --sync-categories --limit 0` | Kategorie w bazach serwisów oraz aktualne etykiety i rekomendacje w bazie wspólnej |
| Eksportuj dane do rekomendacji bez pobierania | `backfill --recommendations --local-only` | Dane zapisane w bazach serwisów i pamięci wcześniejszych odczytów |

```powershell
python main.py backfill --recommendations --local-only
python main.py backfill --recommendations --services storytel bookbeat legimi --limit 1000
python main.py backfill --tags-only
python main.py backfill --tags-only --services empik --recheck --limit 0
python main.py backfill --recommendations --services empik --local-only --limit 0
python main.py backfill --recommendations --recheck --sync-categories --limit 0
```

Tryby zbierania rekomendacji i samych tagów otwierają bazy serwisów tylko do odczytu.
Limit dotyczy rekordów na serwis. Wartość `0` oznacza przejście do końca.
Ponowne uruchomienie tego samego trybu kontynuuje od zapisanego ID. Udane
sprawdzenia kart bez tagów są zapisywane, a nieudane pobrania trafiają do raportu.
Przez `--url` można ponowić sprawdzenie pojedynczego rekordu.
`--tags-only --recheck` pobiera ponownie każdą kartę, omijając także stare
wyniki bez tagów i tagi obecne w archiwum. Ma osobny punkt wznowienia.
Empik korzysta z Playwrighta: tagi ukryte za „Więcej” są już obecne w DOM
i wszystkie są odczytywane bez klikania. Eksport offline przenosi istniejące
tagi z `data\empik.db` do `state\recommendations.db` bez pobierania stron.
Udany ponowny odczyt zastępuje sprawdzone tagi i kategorie aktualnymi etykietami.
Eksport offline uzupełnia brakujące rodzaje etykiet, zachowując istniejące wyniki,
również potwierdzony brak tagów. Błąd pobierania nie usuwa zapisanych danych.

Audyt i uzupełnianie metadanych wykrywają również brak kategorii.
Bazy serwisów zachowują swój schemat: główna kategoria to `books.category_id`,
a LubimyCzytać korzysta z `book_categories`. Pełne listy kategorii trafiają
do `recommendations.db` (`labels.kind = 'category'`). Kategorie i tagi nie są
pobierane z globalnego menu, stopki ani kart polecanych produktów.
Woblink: tagi pochodzą wyłącznie z `#product-card-tags`, również promocyjne
etykiety serwisu. Można je uzupełnić przez `backfill --tags-only --services woblink`.
Słowa kluczowe z JSON-LD własnego produktu są zapisywane jako tagi;
epoki i rodzaje literackie Wolnych Lektur oraz oceny opisowe BookBeat mają
osobne rodzaje etykiet. Brak kategorii w źródle pozostaje brakiem danych.

Wynik jest wspólny dla serwisów i trafia do `state/recommendations.db` obok
pliku stanu `coverage.db` dla danej konfiguracji. Zwykła aktualizacja również
zapisuje tam dostępne sygnały. Wcześniej zebrane tagi z tabel źródłowych i
z archiwum można przenieść przez eksport offline.

W Nokturnie użyj klucza `(site, product_key)`. `product_key` pochodzi z
`urls.identity()` (np. `books.external_id` w LubimyCzytać lub numer z adresu
karty Storytela). Lokalne `books.id` zależy od pliku źródłowego.

| Tabela | Dane |
|---|---|
| `titles` | Adres, nazwa serii, numer tomu i stan sprawdzenia karty |
| `labels` | Tagi, kategorie, epoki i rodzaje literackie z Wolnych Lektur oraz surowe etykiety BookBeat opisujące książkę i narrację |
| `title_details` | Język, czas trwania, ocena i liczba ocen oraz skala, jeśli podało ją źródło. BookBeat ma również osobną ocenę narracji i jej licznik |
| `similar_titles` | Identyfikatory i adresy powiązanych książek, wyłącznie z potwierdzonym ID produktu |
| `related_links` | Dokładny adres relacji, jej rodzaj, nagłówek źródłowy, tytuł, kolejność i informacja o audio, jeżeli źródło ją podało |
| `recommendation_coverage` | Zakres odczytu, liczba relacji, adres źródłowy i następna partia; nie udaje kompletności danych z pojedynczej strony |

```sql
SELECT label FROM labels
WHERE site = :site AND product_key = :product_key AND kind = 'tag'
ORDER BY label
```

`--sync-categories` działa z `--recommendations` i zapisuje wyłącznie sprawdzone
kategorie do istniejących rekordów baz serwisów. Nie przepisuje tytułów,
autorów ani okładek. Naprawia również istniejące błędne powiązania kategorii LC.
Tryb ma osobny punkt wznowienia. Bez tego argumentu bazy serwisów pozostają
w trybach rekomendacji tylko do odczytu. Brak kategorii w źródle nie usuwa
wcześniej zapisanej kategorii. `--recheck` jest potrzebny do ponownego pobrania
kart już sprawdzonych przez starszy parser.

Relacje `similar` pochodzą z odpowiednich sekcji Storytela, Empiku, Woblinka
i Legimi. `read_also` to sekcja czytelników LC, a `also_liked` to publiczna lista
BookBeat. LC często podaje link do opinii: wtedy `related_links.target_key`
pozostaje `NULL`; ID książki nie jest zgadywane z adresu okładki ani ID opinii.
Nieznana dostępność audiobooka ma `audio_available = NULL`; wartość `0`
oznacza wyraźną informację źródła o formatach bez audio. Relacje mogą prowadzić
do pozycji spoza lokalnego katalogu, w tym ebooków. Nokturn powinien uwzględnić
dostępność audio i własne dopasowanie książek przed użyciem ich do rekomendacji.

BookBeat pobiera pierwszą publiczną partię do 25 pozycji. Dalsza paginacja
jest zapisana w `recommendation_coverage.next_url`; `complete = 0` oznacza,
że istnieje następna partia. Dla sekcji HTML `scope = 'page_section'` i
`complete = NULL` oznaczają wyłącznie odczyt pozycji obecnych w dokumencie.
Menu, bestsellery, ostatnio oglądane i ogólne listy promocyjne nie są
przypisywane do podobieństwa konkretnej książki.

Wyniki i ograniczenia audytu wielu przykładów: [SOURCE_SIGNALS_AUDIT.md](SOURCE_SIGNALS_AUDIT.md).

Pełny JSON danych produktu pobranego w zwykłej aktualizacji jest zachowywany
w tabeli `metadata` w `state/<serwis>/coverage.db`. Obejmuje np. ISBN i datę
wydania Storytela oraz liczbę ocen LC, których oryginalne schematy nie mają gdzie
zapisać. Puste odpowiedzi nie kasują wcześniejszych pól dodatkowych.

Błąd jednej strony katalogu odkłada ją o godzinę i pozwala kontynuować inne
kategorie. Odłożona strona blokuje deklarację zakończenia katalogu, również gdy
chwilowo nie ma żadnej strony gotowej do pobrania. Udany odczyt resetuje serię
błędów. Żądania POST paginacji LC ponawiają się także po błędzie sieci.

Nieudane pobranie wymaganej lokalnej okładki oznacza rekord jako niepełny.
Suma SHA-256 dotyczy zapisanego pliku JPEG. Przycisk zatrzymania działa również
podczas tworzenia kopii SQLite. Oceny i daty istniejących recenzji są aktualizowane,
bez dopisywania duplikatów. Audioteka odczytuje datę i jawną ocenę recenzji.
Storytel rozpoznaje zarówno „Seria”, jak i „Serie”; numer tomu jest osobno, a pełna
pozycja np. „1 z 7” zostaje w dodatkowych metadanych.

Raport aktualizacji zawiera `metadata_gaps` oraz `complete`. `complete` wymaga
braku aktywnych błędów kolejki, braków wymaganych metadanych i pozostałych stron
katalogu. Nie jest dowodem dostępności wszystkich tytułów w zewnętrznym serwisie
ani pobrania wszystkich historycznych recenzji.

`verify_pipeline.py` testuje na żywych stronach zapis 12 produktów do izolowanych
kopii oryginalnych schematów, porównuje wartości pól z SQLite, pobiera okładki i
sprawdza pliki oraz sumy kontrolne. Wynik: `reports\pipeline_live.json`.
Dodatkowo przetestowano serię Storytela na bieżącej stronie Harry'ego Pottera
oraz wykonano po jednej rzeczywistej aktualizacji w każdej docelowej bazie,
z kopiami bezpieczeństwa. Pełnego wielogodzinnego uzupełnienia jeszcze nie wykonano.

Pliki zmienione w tej iteracji: `database.py`, `main.py`, `crawl_state.py`,
`discovery.py`, `http_client.py`, `quality.py`, `gui.py`, `config.json`,
`parsers\audioteka.py`, `parsers\storytel.py`, `tests\test_scraper.py`,
`tests\test_coverage.py`, `README.md`. Dodane: `verify_pipeline.py`,
`tests\fixtures\storytel_series_live.html`; raporty w `reports`.


## Równoległa aktualizacja

Domyślnie cztery serwisy pracują jednocześnie, każdy w osobnym procesie.
GUI ma zaznaczoną opcję **Aktualizuj serwisy równolegle**. Odznaczenie uruchamia
serwisy kolejno. Pole **Serwisy jednocześnie** ustala limit 1–10;
CLI, np. `--workers 6`, pozwala uruchomić sześć wybranych źródeł jednocześnie.
Domyślna wartość w `config.json`: `parallel_services: 4`.

Każdy proces ma własne połączenia SQLite, kolejkę, transport HTTP i plik dziennika
`logs\<serwis>.log`. Limit 1 żądania/s obowiązuje osobno dla każdego serwisu.
Tylko proces Empiku uruchamia przeglądarkę. Pobieranie okładek pozostaje włączone.
Bazy główne zachowują schematy. Ten sam serwis nie pracuje w dwóch procesach jednocześnie;
przypadkowe wskazanie tego samego pliku bazy dla różnych serwisów blokuje start.

Stan jest rozdzielony na `state\<serwis>\coverage.db` i `state\<serwis>\run.json`.
Przy pierwszej aktualizacji dotychczasowy wspólny `state\coverage.db` zostaje
skopiowany do baz poszczególnych serwisów z zachowaniem ich metadanych,
ponowień i checkpointów. Wspólna stara baza pozostaje zachowana. `state.json`
pozostaje zbiorczym zapisem; aktualne checkpointy są również zapisywane osobno,
aby przełączenie między pracą równoległą i sekwencyjną nie gubiło postępu.

GUI i Ctrl+C zatrzymują wszystkie aktywne procesy przez wspólny dla danego
uruchomienia sygnał. Rodzic czeka na bezpieczne zakończenie i zbiera raporty,
również częściowe. Błąd jednego serwisu nie przerywa pozostałych. Blokada głównego
scrapera nadal nie pozwala uruchomić dwóch aktualizacji tych samych baz.

Test żywy: cztery serwisy rzeczywiście pracowały w nakładających się przedziałach
czasu, każdy zapisał jeden produkt i pobrał jego okładkę. Empik otworzył widoczną
przeglądarkę i zamknął okno cookies. Wynik: `reports\parallel_live.json`.
`verify_parallel.py` powtarza ten test w izolowanych bazach, bez zmian w bazach
kolekcji. Testy obejmują cztery procesy, migrację stanu, izolację awarii,
zatrzymanie i zapis checkpointów oraz odrzucenie wspólnego pliku bazy.

Pliki tej zmiany: `main.py`, `gui.py`, `events.py`, `config.json`, `README.md`,
`tests\test_scraper.py`. Dodane: `parallel.py`, `worker.py`, `verify_parallel.py`,
`tests\test_parallel.py`.


## Braki danych a zatrzymanie serwisu

Niepełne metadane pojedynczej książki są zapisywane jako `incomplete`, z listą
brakujących pól w stanie serwisu i próbką do 20 rekordów w raporcie
`incomplete_samples`. Nie zwiększają licznika awarii i nie zatrzymują kolejnych
książek. Ponowienia następują z opóźnieniem od godziny do siedmiu dni.
Pobieranie okładki odbywa się również dla rekordów z brakami metadanych.

Pięć kolejnych rzeczywistych błędów pobierania nadal zatrzymuje dany serwis.
GUI odróżnia taki błąd od informacji „Zapisano dane; braki”. Niepełna kolejka
nie jest oznaczana jako w pełni ukończona.

Po instalacji tej poprawki zatrzymaj stary przebieg przyciskiem
**Zatrzymaj bezpiecznie**, zamknij GUI, uruchom `start.bat` i wybierz
**Wznów kolejkę**. Ten tryb pomija ponowne wyszukiwanie linków i przegląd
katalogów, korzystając z już zapisanych zadań. Kolejne zwykłe aktualizacje
nadal wyszukują nowości.

`verify_incomplete_queue.py` odtwarza 15 problematycznych adresów z raportu
z 10 października i po jednej poprawnej książce na każdy z trzech serwisów.
Zapis i okładki sprawdza w izolowanych bazach o oryginalnych schematach.
Wynik: `reports\incomplete_queue_live.json`.


## Oryginalna rozdzielczość okładek Audioteki

Adres okładki jest oczyszczany po parsowaniu JSON-LD oraz przed pobraniem.
Usuwane są parametry zmniejszające obraz (`?w=300`) i końcówki wariantów CDN
po rozszerzeniu obrazu. Pobierany jest plik źródłowy, bez zmniejszania wymiarów.

Tryb GUI **Napraw miniaturki Audioteki** sprawdza istniejące pliki z adresami
wariantów CDN i pliki utworzone przez nowy downloader. Okładki o wymiarach
najwyżej 300 pikseli zastępuje większymi oryginałami. Zachowuje dotychczasowe
ścieżki plików, aktualizuje `books.cover_url` i kopię metadanych w stanie.
Po zaznaczeniu opcji kopii baza i zastępowane pliki są zachowane w
`backups\cover_repair_<data>`. Bez tej opcji robocze pliki są usuwane po naprawie.
Raport jest zapisywany w `reports\audioteka_cover_repair.json`.

Plik z CDN jest weryfikowany przed podmianą; mniejszy obraz, błąd pobierania
lub niezgodny rekord nie powoduje usunięcia starej okładki. Jeśli sam oryginał
ma tylko 300 pikseli, zapisywany jest ten oryginał bez sztucznego powiększania,
a raport wyjaśnia brak większej wersji (`original_small`).
Naprawę można przerwać i wznowić; większe pliki są pomijane. Cztery wątki
pobierają wyłącznie obrazy, a zapis do bazy pozostaje sekwencyjny. Łączny limit
żądań jest dzielony między wątki. Blokada nie dopuszcza równoczesnej aktualizacji.

CLI: `python main.py repair-covers`. Samodzielny wariant:
`python repair_audioteka_covers.py --requests-per-second 4`.


Naprawa wykonana 10 października 2026: zweryfikowano 2421 plików i ich sumy
kontrolne; 2392 okładki zastąpiono większymi oryginałami. 28 plików źródłowych
ma 300×300 również bez parametrów CDN. Nie pozostały adresy wariantów ani
kandydaci do naprawy. Metadane 75 278 książek poza polami okładek, schemat oraz
liczby rekordów wszystkich tabel pozostały bez zmian. `quick_check`: `ok`.
Raport zbiorczy: `reports\audioteka_cover_repair_final.json`. Testy: 59/59.


## Przekierowania UUID Audioteki i pojedyncze błędy

Stare adresy Audioteki `/pl/audiobook/<UUID>/` mogą przekierowywać na adres
z nazwą książki. Poprawny cel w tym samym serwisie jest parsowany, a alias
zapisywany w stanie jako `redirected`. Przy następnym uruchomieniu oba adresy
wskazują ten sam rekord; nie powstaje duplikat i znany alias jest pomijany przy
odkrywaniu nowości. Zwykłe przekierowania na inną książkę lub poza serwis nadal
są odrzucane. Schematy baz głównych nie ulegają zmianie.

Jeżeli cel ma niepełne dane, ponawiany jest adres docelowy; alias nie pozostaje
na stałe jako zadanie `incomplete`. Pojedynczy HTTP 502 nadal jest raportowany
jako błąd i ponawiany z opóźnieniem, a reszta kolejki jest przetwarzana.
GUI pokazuje zakończenie przebiegu z liczbą błędów, odróżniając je od przerwania
po pięciu kolejnych błędach i awarii uniemożliwiającej wykonanie przebiegu.

Na żywo 11 października sprawdzono alias `7a8a297f-e998-4f6d-8521-e9deb9992b68`
(przekierowanie na `roj-dronow-odcinek-1`) oraz `dzikie-serce-tesknoty-meskiej-duszy`.
Oba adresy zwróciły poprawne dane. Raport:
`reports\audioteka_redirect_retry_live.json`. Nie przerywano bieżącego przebiegu
pozostałych serwisów i nie zapisywano do jego baz.


## Automatyczne uzupełnianie podczas pracy innych serwisów

Po swojej pierwszej paczce serwis kontynuuje pracę, jeżeli inne serwisy nadal
pracują. Kolejne paczki obejmują do 25 zadań: brakujące metadane, pozostałą
kolejkę, rozpoczęte katalogi (do 5 stron) oraz rozpoczęty zakres ID LC.
Sitemapy i zwykłe odświeżanie nie są powtarzane. Okładki są pobierane przez
zwykły mechanizm, w tym oryginały Audioteki. Postęp każdej paczki jest zapisany.

GUI pokazuje fazę **Uzupełnianie w tle**, liczniki i końcowy raport sumują
cały przebieg. Po zakończeniu pozostałych serwisów kończy się bieżąca paczka.
Braki i błędy odłożone do ponowienia podczas tego przebiegu nie są ponawiane
w pętli; skan starych braków nie zawija kursora w tle. Pięć kolejnych błędów
pobierania nadal zatrzymuje dany serwis. Przycisk zatrzymania działa także w tle.

Tryby ze wskazanym URL lub limitem zadań zachowują swój zakres. Wznowienie
kolejki kontynuuje wyłącznie zapisane zadania. Praca w tle dotyczy wybranych
serwisów uruchomionych równolegle; najpierw każdy otrzymuje pierwszy przebieg.
Już uruchomiony proces zachowuje wcześniej wczytany kod: ta funkcja działa
po ponownym uruchomieniu GUI i nowym przebiegu.


## Audyt przebiegów z 11–13 października 2026

Praca w tle działała: 13 października Audioteka wykonała 131 dodatkowych
paczek, Storytel 169. Wszystkie ostatnie raporty mają `interrupted=true`;
przerwanie nie oznacza ukończenia katalogów ani napraw wszystkich rekordów.

Poprawki z 14 października:
- LC dopuszcza canonical innego wydania tylko gdy og:url i JSON-LD Book
  potwierdzają żądane ID. Znane książki i zapis są rozpoznawane po external_id,
  także gdy stary URL prowadził do opinii. Przy naprawie taki URL jest poprawiany.
- Z katalogu LC wykluczane są opinie i uszkodzone adresy kategorii, także
  historyczne zadania zapisane w stanie katalogów.
- Empik nie zapisuje obrazka zastępczego jako okładki ani zera jako czasu.
  Brak tabeli szczegółów nie uniemożliwia parsowania pozostałych danych.
  Przeglądarka ponawia HTTP 429 z przerwą 30/60 sekund; jeżeli ograniczenie
  utrzymuje się, serwis kończy pracę, pozostawiając kolejkę do wznowienia.
- Obsługiwane jest zweryfikowane przekierowanie Audioteki projet-hail-mary
  na projekt-hail-mary; inne nierozpoznane przekierowania nadal są sprawdzane.
- Krótszy opis SEO z JSON-LD nie nadpisuje pełnego opisu produktu.
- Raport po przerwaniu uwzględnia zapisane rekordy z przerwanej paczki.

Diagnoza na żywo: `reports\audit_20261014\live.json`, wynik poprawionego
parsowania: `reports\audit_20261014\parsing_after_fix.json`. Testy korzystają
z izolowanych baz o oryginalnych schematach. Audyt nie zmienia głównych baz.


## LC: wydajność opinii i aktualizacja samych nowości

Tabela reviews w bazie LC nie ma indeksu book_id. Dawniej każde sprawdzenie
jednej opinii skanowało całą tabelę. Aktualizator tworzy teraz osobny indeks
identyfikatorów i skrótów opinii w state\<serwis>\<serwis>_reviews.db.
Pierwsze zbudowanie wymaga jednego odczytu istniejących opinii, kolejne
uruchomienia doczytują dopisane ID. Znaleziona opinia jest potwierdzana
w głównej bazie po kluczu głównym przed aktualizacją. Schemat głównej bazy
pozostaje bez zmian; opinie i ich aktualizowane oceny nadal są zapisywane.
Indeks jest pomocniczy i można go odbudować, usuwając plik przy zatrzymanym scraperze.

LC: JSON-LD Audiobook potwierdza tożsamość tak samo jak Book. Dotyczy to
stron audiobooków, których canonical wskazuje wydanie papierowe. Odrzucenie
pojedynczej niezgodnej strony nie jest liczone jako awaria sieci; pozostała
kolejka pracuje dalej. HTTP 429 i serie błędów transportu nadal zatrzymują serwis.

GUI: **Tylko nowości (bez napraw starych rekordów)**. Ten tryb pomija naprawy,
odświeżanie i skan zakresów ID LC; pobiera nieznane książki z wykrytych adresów
i zapisanej kolejki. Okładki są nadal pobierane. Zwykły tryb aktualizacji nadal
uzupełnia metadane starych rekordów. CLI: `python main.py --new-only --fast`.
Pierwsze budowanie indeksu opinii nadal może wymagać czasu. Kopie dużych baz
są od 17 października opcjonalne i wymagają zaznaczenia w GUI albo `--backup`.

## Usprawnienia pracy z 17 października 2026

- GUI odczytuje liczniki wszystkich 10 baz w tle już po otwarciu okna. Aktualizuje
  je w trakcie pobierania, także gdy serwis jeszcze nie zakończył pracy.
- LC ładuje znane identyfikatory z istniejącego indeksu `external_id`, bez
  skanowania opisów książek. Pomiar na 691 781 rekordach: 4,47 s.
- Domyślnie przeglądane jest 5 stron nowości, 5 stron katalogu i 2 mapy na porcję.
  Dalsze strony i mapy mają trwały punkt wznowienia. Przegląd jest przeplatany
  pobieraniem porcji 100 książek. Praca trwa również po zakończeniu innych serwisów.
  Dotyczy to zwykłej aktualizacji bez limitu; podany limit nadal jest respektowany.
- Woblink: błędy 500 pustych kart `/audiobook/--…` są odkładane do ponowienia
  i nie blokują pozostałych książek. Szczegóły są w `deferred_errors` raportu.
- Publio: po HTTP 403 próbowana jest zwykła przeglądarka z osobnym profilem.
  Jeśli serwis nadal odmawia dostępu, scraper zatrzymuje ten serwis i zachowuje
  kolejkę, zamiast ponawiać blokowane żądania dla następnych książek.
- BookBeat: żądanie dostępnej partii opinii ma limit 8 s i jedną próbę.
  Niepowodzenie nie usuwa metadanych książki. Pominięte produkty są widoczne w GUI.
- Wolne Lektury: brak opisu nie jest błędem, ponieważ API wielu utworów go nie
  publikuje. Pełna lista audio jest nadal porównywana z bazą przy aktualizacji.
- Virtualo: jeśli karta ma jedynie skrót opisu SEO, jest on zachowany z oznaczeniem
  `description_kind=seo_excerpt` w pomocniczych metadanych.
