ME7.5 SELECTIVE LAUNCH CONTROL — GUI W STYLU MAPSWITCH

ZAWARTOŚĆ
Launch_MultiMap_Selective_GUI_FINAL_v3.py — kompletny program.
launch_multimap.ui — interfejs do edycji w Qt Designer.
requirements.txt — zależność nowego GUI.

URUCHOMIENIE (LINUX / WINE)
Wymagany Python 3.10 lub nowszy, PySide6 oraz działający Wine.
Otwórz terminal w rozpakowanym folderze:

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 Launch_MultiMap_Selective_GUI_FINAL_v3.py

Jeśli używasz już środowiska Python z PySide6, wystarczy ostatnia komenda.
Plik .py działa również samodzielnie: zawiera wbudowany interfejs.
Jeśli obok niego znajduje się launch_multimap.ui, program używa tego pliku.
Zachowaj nazwy obiektów podczas edycji w Qt Designer.

OBSŁUGA
1. Wskaż folder z ME7Check.exe, ME7Info.exe i me7sum.exe.
2. Wybierz MultiMap BIN o rozmiarze 1 MB oraz odpowiadający mu log CSV.
3. Klikaj przyciski banków: niebieski oznacza aktywne LC.
4. Ustaw parametry niezależnie dla każdego wybranego banku.
5. Naciśnij PATCH & GENERATE CSOK BIN.

Analiza BIN uruchamia się po jego wybraniu. Można ją powtórzyć przyciskiem
Analyze BIN w zakładce ECU info / process log.
Program wykonuje dotychczasową sekwencję ME7Check, ME7Info, LC patch,
generowanie XDF, ME7Sum i końcowy ME7Check. Pliki powstają obok wejściowego BIN.
Przycisk Open folder pojawia się po zakończeniu procesu.

ZMIANY I SPRAWDZENIE
Jasny interfejs Qt, karty i kolory na podstawie dostarczonego main.ui,
jeden główny przycisk PATCH, widoczne parametry wszystkich banków,
oddzielna zakładka danych ECU i logu oraz pasek postępu.
Podczas pracy pola wejściowe są zablokowane, a zadania nie nakładają się.
Błędy pól są wskazywane przed uruchomieniem patchowania.

Funkcje patchowania, generowania kodu ECU/XDF i obsługi sum kontrolnych
pozostały bez zmian. Porównano strukturę całego zachowanego backendu.
50 porównań na danych syntetycznych dało identyczne bajty BIN: wszystkie
kombinacje banków dla 2–4 banków, z oryginalnym hookiem i łańcuchem CALLS.
Sprawdzono też GUI, obsługę błędów i przebieg procesu z symulowanymi EXE.
Nie wykonano tutaj próby z prawdziwymi narzędziami ME7 ani na sterowniku.
