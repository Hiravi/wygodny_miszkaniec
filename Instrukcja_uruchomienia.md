# Instrukcja uruchomienia

Wygodny Mieszkaniec

## 1. Wymagania

| Element | Wymaganie lub zalecenie |
| --- | --- |
| System i procesor | Windows x64 na procesorze Intel lub AMD. Uruchomienie sprawdzono na Windows 11. |
| Pamięć operacyjna (RAM) | Zalecane 16 GB. W pomiarze trzy procesy aplikacji zużywały łącznie około 7 GB po zakończeniu inicjalizacji, bez przeglądarki i systemu. Minimalnej konfiguracji nie zweryfikowano. |
| Miejsce na dysku | Zalecane co najmniej 6 GB wolnego miejsca na archiwum, rozpakowane pliki i pliki tymczasowe. Sam rozpakowany pakiet zajmuje około 2,5 GB. |
| Przeglądarka | Aktualna przeglądarka z obsługą WebGL 2 (Web Graphics Library), np. Chrome, Edge lub Firefox. |
| Porty lokalne | Wymagane wolne porty 5173, 8000, 8800 i 8810. |
| Połączenie z internetem | Nie jest wymagane do uruchomienia. Katalog stacji i bieżące odczyty Głównego Inspektoratu Ochrony Środowiska wymagają połączenia. |

Python, Java i przygotowane grafy transportowe są w pakiecie. Nie trzeba instalować ich osobno.

## 2. Uruchomienie

1. Pakiet należy rozpakować w całości do lokalnego folderu z prawem zapisu.
2. Z rozpakowanego folderu należy uruchomić `Start.cmd` i pozostawić otwarte okno konsoli.
3. Jeżeli przeglądarka nie otworzy się automatycznie, aplikację można otworzyć pod adresem [http://127.0.0.1:5173](http://127.0.0.1:5173). Silniki trasowania mogą być gotowe później niż interfejs.

## 3. Zatrzymanie i usunięcie

Aby zatrzymać aplikację, należy wpisać `stop` w jej konsoli i zatwierdzić klawiszem Enter. Po zakończeniu procesów można zamknąć kartę w przeglądarce i usunąć rozpakowany folder (wraz z `.state/`) oraz pobrane archiwum. Jeśli pliki trafiły do Kosza, można je stamtąd usunąć na stałe. Dane witryny `http://127.0.0.1:5173` można wyczyścić w ustawieniach przeglądarki.
