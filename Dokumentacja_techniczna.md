# Dokumentacja techniczna

Wygodny Mieszkaniec

## 1. Wykorzystane technologie

| Technologia lub biblioteka | Rola w projekcie |
| --- | --- |
| TypeScript | Język interfejsu; typy pomagają wykrywać niespójności w kodzie i strukturach danych przed uruchomieniem. |
| React i React DOM | React buduje elementy interfejsu i zarządza ich stanem. React DOM wyświetla je w modelu dokumentu przeglądarki (Document Object Model, DOM). |
| Node.js i Vite | Node.js uruchamia narzędzia do budowania interfejsu, a Vite przygotowuje gotowe pliki przeglądarkowe. Nie są potrzebne do uruchomienia dostarczonego pakietu. |
| MapLibre GL JS | Rysuje interaktywną mapę, podkład i warstwy tematyczne w przeglądarce za pomocą WebGL 2 (Web Graphics Library). |
| Zod | Sprawdza, czy odpowiedzi otrzymane przez przeglądarkę mają oczekiwaną strukturę. |
| Python (CPython) | Uruchamia serwer aplikacji, program startowy i narzędzia przetwarzania danych. |
| FastAPI, Starlette i Uvicorn | FastAPI udostępnia interfejs programistyczny aplikacji (Application Programming Interface, API). Starlette udostępnia gotowe pliki interfejsu. Uvicorn nasłuchuje na portach lokalnych i obsługuje żądania. |
| Pydantic | Sprawdza parametry żądań i struktury odpowiedzi po stronie serwera. |
| HTTPX | Klient używany przez API do wysyłania żądań do usługi obliczającej zasięg. |
| SQLite | Przechowuje lokalne dane analityczne, indeks adresów oraz część przygotowanych kafli. Indeksy przestrzenne przyspieszają wyszukiwanie obiektów według położenia. |
| Shapely i pyproj | Shapely wykonuje operacje na geometriach, np. sprawdza, czy punkt leży w danym obszarze. Pyproj przelicza współrzędne między układami odniesienia. |
| pandas i GeoPandas | pandas przetwarza dane tabelaryczne. GeoPandas łączy tabele z geometrią, m.in. podczas przygotowania punktów używanych w obliczeniach zasięgu. |
| Java (Eclipse Temurin) | Środowisko wykonawcze dla silników trasowania. |
| OpenTripPlanner (OTP) | Silnik trasowania używany m.in. do obliczania tras pieszych między dwoma punktami. |
| R5 i r5py | R5 liczy czasy podróży transportem zbiorowym oraz zasięg z wybranego punktu. r5py udostępnia funkcje R5 kodowi w Pythonie. |

## 2. Dane wykorzystywane w projekcie

### Integracja danych i sposób ich prezentacji

Projekt łączy na jednej mapie Krakowa dane m.in. z Miejskiego Systemu Informacji Przestrzennej (MSIP) Urzędu Miasta Krakowa, Zarządu Transportu Publicznego, OpenStreetMap, Głównego Urzędu Statystycznego (GUS), Głównego Inspektoratu Ochrony Środowiska (GIOŚ) i Głównego Urzędu Geodezji i Kartografii. Dzięki temu można np. zestawić rozmieszczenie parków i usług z gęstością zaludnienia oraz zasięgiem dojazdu według rozkładu. Do analizy ludności służy m.in. siatka GUS o rozdzielczości 125 m.

Dane są sprawdzane i przetwarzane przed uruchomieniem aplikacji. Powstają bazy analityczne, agregaty i kafle mapowe. Podczas pracy przeglądarka pobiera z lokalnego serwera kafle dla bieżącego widoku oraz szczegóły wybranego miejsca. Ogranicza to ilość przesyłanych danych i obciążenie urządzenia użytkownika.

| Dane i źródło | Zastosowanie w aplikacji |
| --- | --- |
| [OpenStreetMap (OSM)](https://www.openstreetmap.org/copyright) | Podkład mapy, budynki i ich funkcje, sieć piesza, infrastruktura rowerowa oraz punkty usługowe, m.in. sklepy, apteki, kościoły i parkingi. Granice parków i lasów z OSM tworzą warstwę „Zieleń” i wspierają analizę dojścia do tych terenów. |
| Urząd Miasta Krakowa, Miejski System Informacji Przestrzennej (MSIP): [adresy](https://msip.um.krakow.pl/Dane/Adresy_JSON.zip), [dzielnice](https://msip.um.krakow.pl/Dane/Dzielnice_JSON.zip), [obszary SIM](https://msip.um.krakow.pl/arcgis/rest/services/ZSOZ/Rc_Sim/MapServer/0) | Ewidencja Miejscowości, Ulic i Adresów (EMUiA) zasila wyszukiwarkę. Granice wyznaczają dzielnice i obszary Systemu Informacji Miejskiej (SIM), prezentowane jako „Okolica”. |
| [Zarząd Transportu Publicznego w Krakowie (ZTP)](https://gtfs.ztp.krakow.pl/) | Przystanki, rozkłady i kalendarze w formacie General Transit Feed Specification (GTFS). Z połączenia rozkładów z siecią pieszą OSM powstają grafy transportowe do tras i obliczeń zasięgu. |
| [Strategiczna mapa hałasu MSIP](https://msip.um.krakow.pl/Dane/Mapa_Halasu_2022_JSON.zip) | Nocny hałas drogowy z mapy z 2022 r. służy do prezentacji warstwy „Hałas” i wskaźnika ciszy dla wybranego punktu. |
| Główny Inspektorat Ochrony Środowiska (GIOŚ): [model roczny](https://wody.gios.gov.pl/arcgis/rest/services/PJP2_MapServices/ModelowanieNaPotrzebyOcen/MapServer/2), [archiwum pomiarów](https://powietrze.gios.gov.pl/pjp/archives/downloadFile/644), [katalog i pomiary bieżące](https://powietrze.gios.gov.pl/pjp/content/api) | Model przestrzenny średniego stężenia drobnego pyłu zawieszonego (PM2.5) za 2025 r., historia pomiarów dla dwóch stacji i bieżące odczyty. Historia stacji nie jest miesięczną mapą całego miasta. |
| [Główny Urząd Statystyczny (GUS)](https://geo.stat.gov.pl/atom-web/atom/GRID), Narodowy Spis Powszechny Ludności i Mieszkań (NSP) z 2021 r. | Liczebność i gęstość ludności w siatce 125 m oraz grupy wieku w odrębnej siatce 1 km. Dane o wieku nie są przenoszone na siatkę 125 m. |
| [Rejestr Cen Nieruchomości (RCN), eksport Głównego Urzędu Geodezji i Kartografii](https://opendata.geoportal.gov.pl/InneDane/latest_exports/rcn_transakcje_ceny/PARQUET/12_transakcje_ceny_lokale.parquet) | Mediany cen za metr kwadratowy lokali mieszkalnych na podstawie dokumentów z lat 2025-2026. Aplikacja pokazuje wyniki łączne i z podziałem na rynek pierwotny oraz wtórny. Gdy próba lokalna jest zbyt mała, może pokazać odniesienie dla dzielnicy. |
| [Rejestr Empatia](https://rejestrzlobkow.mrpips.gov.pl/) | Uzupełnia katalog miejsc o informacje o opiece nad dziećmi, m.in. żłobkach i klubach dziecięcych. |

## 3. Architektura rozwiązania

![Schemat przepływu żądań: przeglądarka łączy się z lokalnym serwerem aplikacji, który odczytuje dane mapowe, korzysta z silników OpenTripPlanner i R5 oraz pobiera bieżące dane z GIOŚ.](Schemat_architektury.svg)

Rys. 1. Przepływ żądań w aplikacji lokalnej. Linia przerywana oznacza połączenie internetowe.

### Przygotowanie danych

Najpierw skrypty pobierają dane z portali źródłowych, sprawdzają ich strukturę i normalizują współrzędne. Następnie zapisują wyniki w dwóch postaciach: danych analitycznych do wyszukiwania i obliczeń oraz kafli do wyświetlania. Osobno z sieci OSM i rozkładów GTFS przygotowywane są grafy dla silników trasowania. Pakiet zawiera gotowe pliki, więc standardowy start nie wymaga budowania grafów od początku.

### Komponenty uruchomieniowe

| Komponent | Adres i port lokalny | Zadanie |
| --- | --- | --- |
| Interfejs w przeglądarce | `127.0.0.1`, port **5173** | Punkt wejścia użytkownika. Ten adres udostępnia gotowy interfejs i obsługuje jego żądania do API. |
| Serwer aplikacji | `127.0.0.1`, port **8000** | Drugi port tego samego procesu Uvicorn, używany m.in. przez program startowy do sprawdzenia gotowości. |
| OpenTripPlanner | `127.0.0.1`, port **8800** | Osobny proces trasowania, wywoływany przez serwer aplikacji. |
| Usługa R5 | `127.0.0.1`, port **8810** | Osobny proces obliczający m.in. zasięg transportu zbiorowego. |

### Przepływ żądań podczas pracy

1. **Uruchomienie.** Program startowy uruchamia serwer, OpenTripPlanner i R5. Mapa może działać, zanim silniki wczytają grafy.
2. **Zapytania z przeglądarki.** MapLibre pobiera kafle dla aktualnego widoku. Serwer obsługuje też wyszukiwanie adresów oraz zapytania o punkty i obszary.
3. **Odpowiedź lokalna.** Serwer czyta przygotowane pliki lub wywołuje odpowiedni silnik, a wynik zwraca przez API. Obliczenia opisano w rozdziale 4.
4. **Dane bieżące.** Serwer pobiera katalog stacji i odczyty GIOŚ przez internet. Niedostępność GIOŚ lub silnika ogranicza powiązaną funkcję; przygotowane warstwy pozostają dostępne.

### Struktura archiwum i katalogów

Ścieżki są względne względem rozpakowanego katalogu aplikacji `Wygodny-mieszkaniec/`. Układ katalogów należy zachować.

| Najważniejsze pliki i foldery | Zawartość i rola |
| --- | --- |
| `Start.cmd`, `tools/release/` | Start w systemie Windows, uruchamianie procesów i sprawdzanie pakietu. |
| `apps/web/dist/` | Gotowy interfejs, skrypty, style i ikony; do startu nie jest potrzebny Node.js. |
| `services/api/`, `tools/data/` | Kod serwera i współdzielone moduły odczytu oraz przetwarzania danych. |
| `tools/router/` | Integracja silników, obsługa dojść, przesiadek i kalendarzy. |
| `runtime/python/` | Dołączony interpreter Python i jego biblioteki. |
| `.cache/router/` | Dołączona Java, silniki i gotowe grafy. Katalog jest potrzebny do trasowania. |
| `data/derived/generated/` | Lokalne bazy SQLite, geometrie i podsumowania w plikach GeoJSON lub JSON oraz gotowe kafle. |
| `data/manifests/`, `ZRODLA_DANYCH.csv` | Pochodzenie, okresy, sumy kontrolne i opis przekształceń danych. |
| `MINIMAL_RUNTIME.json`, `DEPENDENCIES.json` | Wykaz plików i ich sum kontrolnych SHA-256 oraz informacje o zależnościach. |
| `README_PL.txt`, `OPIS_APLIKACJI_PL.pdf` i dokumentacja techniczna | Instrukcja uruchomienia, opis celu i funkcji oraz opis techniczny. |
| `LICENCJE/` | Materiały licencyjne i wykaz składników. |
| `.state/` | Dzienniki i pliki tymczasowe tworzone po uruchomieniu; nie należą do archiwum wejściowego. |

Pakiet wykonawczy zawiera wyniki przygotowania danych. Nie obejmuje pełnego repozytorium ani surowych, jednostkowych zapisów Rejestru Cen Nieruchomości (RCN).

## 4. Przygotowanie danych, obliczenia i wizualizacja

### Wspólne etapy przygotowania

1. **Pozyskanie danych.** Zapisywane są źródło, okres danych, data pobrania i suma kontrolna wejścia. Okres, którego dotyczy wartość, pozostaje odrębny od daty pobrania i przetworzenia.
2. **Sprawdzenie i normalizacja.** Kontrolowane są wymagane pola, jednostki, identyfikatory, geometrie i zakres wartości. Ujednolica się kategorie i usuwa duplikaty. Brak wartości oznacza brak danych, a nie zero.
3. **Przeliczenie współrzędnych.** GeoJSON posługuje się długością i szerokością geograficzną w układzie WGS 84 (World Geodetic System 1984, EPSG:4326). Do obliczania odległości i buforów w metrach stosowany jest m.in. układ EPSG:2180. Siatki statystyczne GUS są odtwarzane w układzie EPSG:3035, a kafle mapy korzystają z układu Web Mercator (EPSG:3857).
4. **Przygotowanie plików.** Z oczyszczonych danych powstają lokalne bazy SQLite z indeksami przestrzennymi R-tree, zestawienia dla obszarów i kafle mapowe. Geometria analityczna jest oddzielona od tej używanej do rysowania mapy.

### Hałas i jakość powietrza

**Hałas.** Zakresy nocnego hałasu drogowego z mapy strategicznej z 2022 r. są porządkowane według dolnej i górnej granicy w decybelach. Odczyt w punkcie wynika z przecięcia z geometrią analityczną. Kolor oznacza pasmo; nie jest odrębnym pomiarem.

**Powietrze.** Z rocznego modelu stężenia PM2,5 za 2025 r. przygotowano osobną warstwę rastrową. Interpolacja dwuliniowa wygładza obraz, a maska pozostawia bez wartości komórki nieznane i obszary poza pokryciem. Odczyt analityczny w punkcie nadal korzysta z oryginalnych wartości modelu. Historia dwóch stacji jest dołączona lokalnie; wybór stacji zależy od internetowego katalogu GIOŚ. Bieżące odczyty to odrębne dane punktowe. Zmiana miesiąca historii nie przełącza mapy rocznej.

### Ludność i grupy wieku

Oryginalne komórki zaludnienia GUS mają bok 125 m. Przy większym oddaleniu mapy liczebność w okolicy jest szacowana przez przecięcie komórek z granicami Systemu Informacji Miejskiej (SIM): liczebność komórki mnoży się przez udział jej powierzchni w obszarze, a wyniki sumuje. Zakłada to równomierne rozmieszczenie ludności wewnątrz komórki. Gęstość oblicza się dla znanej powierzchni, bez przenoszenia wyniku na obszary bez danych.

Udziały grup wieku 0-14, 15-64 i 65+ liczy się oddzielnie w oryginalnej siatce 1 km, dzieląc liczebność grupy przez liczbę mieszkańców komórki. Niespójne sumy i braki oznacza się jako nieznane. Dane o wieku nie są przeliczane na komórki 125 m.

### Ceny mieszkań

Dane wejściowe RCN są normalizowane i sprawdzane pod kątem m.in. rodzaju lokalu i rynku, pełnego udziału, dodatniej ceny i powierzchni oraz identyfikatorów i geometrii. Powtórzenia rozstrzyga się według identyfikatorów transakcji i lokalu. Do prezentowanych agregatów nie trafiają rekordy oznaczone przez reguły kontroli.

**Cena jednostkowa = cena brutto lokalu / powierzchnia użytkowa.** Aplikacja pokazuje medianę zakwalifikowanych cen w zł/m², gdy grupa obejmuje co najmniej 5 różnych transakcji. Jeżeli danych dla okolicy jest za mało, można pokazać odniesienie dla dzielnicy; przy braku obu prób wartość pozostaje nieznana. Rynek łączny, pierwotny i wtórny liczone są osobno. Okres 2025-2026 odnosi się do dat dokumentów. Wynik jest statystyką obszaru, a nie wyceną konkretnego lokalu.

### Zieleń, miejsca, zabudowa i infrastruktura rowerowa

Aktywna warstwa zieleni pokazuje parki i lasy z OpenStreetMap. Miejsca są przypisywane do kategorii i deduplikowane z zachowaniem identyfikatorów źródłowych. Punkt reprezentujący obiekt na mapie nie jest potwierdzonym wejściem. Przeznaczenie budynków i rodzaje infrastruktury rowerowej, np. drogi, pasy i odcinki współdzielone, wynikają z dostępnych oznaczeń w danych.

### Wskaźniki wybranego punktu

| Wskaźnik | Sposób obliczenia |
| --- | --- |
| Cisza nocą i jakość powietrza | Odczyt w punkcie porównuje się z wartościami w środkach zamieszkanych komórek siatki 125 m. Percentyl uwzględnia wszystkie niższe wartości i połowę remisów. Dla obu wskaźników wynik to 100 minus percentyl, ponieważ niższe wartości źródłowe oznaczają korzystniejsze warunki. |
| Zieleń | Powierzchnie parków i lasów łączy się, aby uniknąć podwójnego liczenia. Liczy się ich udział w kole 500 m oraz odległość w linii prostej od najbliższej geometrii. |
| Codzienne usługi | Liczy się odległość do najbliższego znanego obiektu w każdej z pięciu kategorii: sklep spożywczy, apteka, opieka zdrowotna, bankomat i targ. Kategoria daje 1 punkt do 500 m lub 0,5 punktu powyżej 500 m i do 1 km. |
| Transport | W promieniu 600 m zlicza się różne kursy w zapisanym dniu, w godzinach 07:00-19:00. Liczba kursów podzielona przez 12 daje średnią na godzinę. Wybierany jest niższy z poziomów częstotliwości i bliskości przystanku. |

Wskaźniki są prezentowane osobno. Jeśli katalog jest niepełny, wynik może być dolną granicą znanej wartości; brak danych nie oznacza braku usług, zieleni ani połączeń.

### Trasy i zasięg transportu

Przed uruchomieniem przygotowuje się grafy łączące sieć pieszą OpenStreetMap z rozkładami, przystankami i kalendarzami GTFS. Obliczenia korzystają z wybranej daty i godziny w strefie `Europe/Warsaw`, uwzględniając marsz po dostępnej sieci, oczekiwanie, przejazd i przesiadki. Czasy wynikają z dołączonego rozkładu i nie uwzględniają bieżących opóźnień.

Zasięg liczy się do 45 minut. Widoki 15 i 30 minut pokazują części tego wyniku: zasięg 15 min zawiera się w zasięgu 30 min, a ten w zasięgu 45 min. Szacunki dla próbek terenu rozmieszczonych co 150 m są odróżniane od potwierdzonych przystanków i odcinków przejazdu; nie określają dostępności każdego adresu w obrębie komórki.

Jeżeli w odległości 30 m nie można powiązać początku z siecią pieszą, aplikacja może zaproponować punkt sieci w promieniu do 200 m. Obliczenie zaczyna się tam dopiero po wybraniu opcji „Oblicz od najbliższej ulicy”. Nie dolicza się niepotwierdzonego dojścia od adresu do tego punktu.

### Przygotowanie obrazu mapy i dostarczanie danych

Kafle wektorowe Mapbox Vector Tile (MVT) zawierają geometrię i atrybuty wycinka, a kafle rastrowe Portable Network Graphics (PNG) zawierają gotowy obraz, np. rocznej warstwy powietrza. GeoJSON jest używany m.in. do przekazywania geometrii wybranych obszarów. Przycinanie i upraszczanie geometrii na potrzeby obrazu nie zmienia źródła obliczeń.

Przeglądarka pobiera kafle dla wybranego obszaru i poziomu zbliżenia. Dzięki temu w przeglądzie można pokazać agregaty SIM, a po przybliżeniu oryginalne komórki ludności o boku 125 m. Szczegóły punktu trafiają w osobnej odpowiedzi API. Liczba przechowywanych odpowiedzi jest ograniczana, a zmiana wyboru pozwala anulować nieaktualne zapytanie. Źródło i okres danych można sprawdzić w katalogu `ZRODLA_DANYCH.csv` oraz w metadanych z `data/manifests/`.

## 5. Możliwości rozwoju projektu

### Rozszerzenie danych i obliczeń

Można zautomatyzować pobieranie nowych rozkładów GTFS i danych miejskich, z kontrolą kompletności, daty publikacji i wyników na trasach referencyjnych. Uzupełnienie sieci o potwierdzone wejścia, przejścia i bariery pozwoli dokładniej szacować dojście od adresu. Na tej podstawie można dodać obliczenia dla wielu adresów oraz wyszukiwanie lokalizacji spełniających limit czasu dojazdu do pracy, szkoły lub innych wskazanych miejsc.

### Analiza zmian w czasie

Projekt można rozszerzyć o zapisywanie historycznych danych wraz z okresami, których dotyczą. Tam, gdzie dostępne dane są wystarczające i porównywalne, aplikacja mogłaby obliczać statystyki miesięczne i roczne, a następnie prezentować je na wykresach lub na mapie z wyborem okresu. Pozwoliłoby to np. porównać jakość powietrza przy danej stacji między miesiącami albo mediany cen mieszkań w tej samej dzielnicy między latami. Wyniki wskazywałyby braki danych i zmiany sposobu pomiaru, które ograniczają porównanie.

### Wdrożenie w internecie

Pierwsze wdrożenie może działać na jednym serwerze z Linuksem. Interfejs, API i silniki trasowania można uruchomić w oddzielnych kontenerach z przygotowanymi plikami danych. Wymaga to środowiska serwerowego zamiast lokalnego programu startowego dla Windows. Przeglądarka łączyłaby się z jedną domeną przez szyfrowany protokół HTTPS (Hypertext Transfer Protocol Secure). Serwer wejściowy udostępniałby pliki interfejsu oraz kierował zapytania do API; silniki trasowania pozostawałyby dostępne tylko w sieci wewnętrznej. Kafle, bazy i grafy można przygotowywać poza serwerem obsługującym ruch i wymieniać po sprawdzeniu. Przed publikacją należy zweryfikować warunki udostępniania danych i dołączonych zależności.

### Skalowanie

Gotowe pliki i kafle można buforować i dostarczać niezależnie od obliczeń. W razie wzrostu ruchu można uruchomić dodatkowe instancje API z kopiami baz tylko do odczytu oraz osobno zwiększać liczbę instancji silników trasowania. Każda instancja silnika potrzebuje pamięci na własny graf, więc przepustowość zależy od wielkości danych, liczby jednoczesnych obliczeń i dostępnej pamięci. Kosztowne zapytania można kolejkować i ograniczać ich równoległość. Przed doborem infrastruktury należy zmierzyć czasy odpowiedzi i zużycie zasobów pod obciążeniem. Dodanie kolejnego miasta wymaga danych porównywalnej jakości i osobnego przygotowania lokalnych warstw oraz grafów.

### Mapa jako część większej platformy

Mapa może stać się punktem wejścia do zamawiania szczegółowych raportów dla wskazanego adresu, obszaru lub zestawu lokalizacji. Osoba prywatna mogłaby otrzymać porównanie codziennych dojazdów, usług, zieleni, hałasu i cen w okolicy. Agencja nieruchomości mogłaby osadzić skróconą kartę w ogłoszeniu, a firma rozważająca nową placówkę zamówić zestawienie dostępności transportowej, ludności i usług w kilku miejscach. Raporty i karty korzystałyby z tych samych danych co mapa, z podaniem źródeł, okresów i ograniczeń. Generowanie raportów można uruchamiać w tle, niezależnie od bieżącego korzystania z mapy.
