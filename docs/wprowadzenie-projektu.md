# Monika – wprowadzenie do projektu Claude (stan na 3.11.2026)

Ten dokument otwiera osobny projekt Claude dla Moniki. Wcześniej Monika była rozwijana we wspólnym
wątku z Markiem (agent Brandwise). Od teraz projekty są rozdzielone: tutaj wyłącznie Monika.

Na początku każdej pracy przeczytaj w repo `KancelariaBil/kancelaria-agent`: `CLAUDE.md` (zasady –
nadrzędne), `docs/stan.md` (co działa, co otwarte), `docs/plan.md` (etapy), `docs/instalacja.md` (serwer).

---

## 1. Kto prowadzi projekt i jak z nim pracować

- **Łukasz Świrgał** (lukasz@brandwise.pl) – jedyny autor projektu, prokurent spółki spolkazoo.online.
  Sam decyduje o kształcie Moniki; nie potrzebuje niczyjej zgody na zmiany w systemie. Jest też
  „osobą zatwierdzającą” w rozumieniu `CLAUDE.md` do czasu przekazania kancelarii.
- Docelowo projekt przejmuje kancelaria (konto Claude Basi), a Łukasz odcina sobie dostępy – dopiero
  gdy sam o to poprosi.
- Styl współpracy:
  - po polsku, zwięźle, od razu do rzeczy, bez wstępów i podsumowań;
  - „nie wiem” w pierwszym zdaniu, gdy czegoś nie wiesz; oddzielaj sprawdzone od przypuszczeń;
  - kwestionuj założenia, gdy widzisz problem – partner, nie wykonawca;
  - przy wątpliwościach co do zakresu polecenia – zapytaj, zamiast interpretować po swojemu;
  - Łukasz nie czyta szczegółów technicznych: sam testuj i wdrażaj do skutku, jemu podawaj wynik;
  - komendy dla Łukasza: gotowe, kompletne, z informacją gdzie wkleić (Mac `%` czy serwer `ubuntu@…:~$`);
  - zabezpieczeń zawężających dostęp (logowanie, hasła, blokady) nie wprowadzaj sam – najpierw zapytaj
    Łukasza o zgodę i formę (dotyczy np. panelu WWW Moniki);
  - nie wpisujesz haseł za Łukasza; w linkach w mailach – zwykłe URL-e (konektor Gmail zamienia linki
    na przekierowania google.com/url – szkice z linkami twórz w Gmailu w przeglądarce).

## 2. Czym jest Monika

Asystentka AI **Kancelarii Adwokackiej adw. Barbary Bil** (kancelariabil.pl) – prawo spółek (głównie
sp. z o.o.), umowy handlowe, spory gospodarcze, fundacje rodzinne. Działa jak pracownik z własną skrzynką:
zespół pisze do niej zlecenia mailem, ona odpowiada wynikiem.

- **Skrzynka Moniki:** biuro@kancelariabil.pl (Google Workspace kancelarii, tylko dla Moniki – nie
  podawać klientom).
- **Kto może zlecać (`team` w config.yaml):** barbara.bil@kancelariabil.pl (Basia, adwokat – pierwsza
  osoba w zespole, dostaje raporty i zgłoszenia o obcych mailach) oraz lukasz@brandwise.pl (pełny dostęp
  na czas budowy i testów, decyzja z 28.10.2026). Aneta i Agnieszka – do dopisania, adresy niepotwierdzone.
- Monika **nigdy nie pisze do klientów, sądów ani drugiej strony** – przygotowuje teksty dla zespołu.
- Co umie (gotowe w kodzie):
  - analiza załączników: PDF (pdftotext), Word, Excel, doc/odt/rtf (LibreOffice), skany (pdftoppm → obraz);
  - projekty pism .docx ze wzorów kancelarii (`data/wzory`, import `wzory-import wzory.zip`);
  - odsetki – wyłącznie deterministyczny kalkulator `odsetki.py` (tabela stóp NBP sprawdzona 20.10.2026;
    handlowe liczone od stopy z półrocza);
  - research tylko w oficjalnych źródłach (`OFFICIAL_DOMAINS`: ISAP, SN, NSA, KIS…);
  - pamięć spraw, osób i firm; dostęp do innych spraw tylko tego samego klienta;
  - diagnostyka: mail od osoby z zespołu o temacie „diagnostyka” → raport stanu bez modelu AI.
- Zaufanie do nadawcy: tylko z podpisem DKIM domeny nadawcy albo DMARC=pass (samo SPF nie wystarcza).
- Tryb próbny (`dry_run`) **wyłączony** od 20.10.2026 – odpowiedzi wychodzą naprawdę, tylko do `team`.

**Odrębność:** kancelaria (Kancelaria Bil) i spolkazoo.online to odrębne spółki (ci sami ludzie) – nie
łączyć danych, płatności ani kont. Monika nie ma nic wspólnego z Markiem ani systemami Brandwise.

## 3. Infrastruktura

| Element | Stan |
| --- | --- |
| Serwer | OVH VPS-1 (2 vCore, 4 GB RAM), Kanada (BHS), `vps-6da277d8.vps.ovh.ca`, IP **54.39.231.254**, Ubuntu 24.04, użytkownik `ubuntu`, katalog `~/kancelaria-agent` |
| Konto OVH serwera | **xa11043-ovh** („Kancelaria Bil sp. z o.o.”, maile na kontakt@spolkazoo.online). Uwaga: Basia ma też konto ng137272-ovh (barbara.bil@) – bez VPS. Panel: manager.eu.ovhcloud.com |
| Usługi | systemd `monika`; `monika-update.timer` co 5 min pobiera `main` i restartuje (restart tylko gdy testy przechodzą) |
| Repo | GitHub **KancelariaBil/kancelaria-agent** – obecnie **publiczne** (do decyzji Łukasza, zalecane prywatne; sekretów w nim nie ma). Serwer pobiera kod kluczem tylko do odczytu (Deploy key). Lukasz904 – collaborator z prawem zapisu |
| Poczta | Gmail OAuth (XOAUTH2), projekt Google Cloud „monika-poczta” (ekran zgody wewnętrzny), klient OAuth `10144385402-a9u0sn4aahsq6imlff79us9cccq06ddi.apps.googleusercontent.com`. Admin Workspace: barbara.bil@kancelariabil.pl |
| DNS kancelariabil.pl | MyDevil, panel23.mydevil.net, konto „brand” (należy do Brandwise – do przeniesienia na kancelarię). Rekordy: `google._domainkey` (DKIM), `_dmarc` (`v=DMARC1; p=none`) |
| Sekrety | tylko `~/kancelaria-agent/.env` na serwerze (ANTHROPIC_API_KEY, GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN, AGENT_EMAIL) |

**Dostęp SSH (do potwierdzenia na starcie!):** 28.10.2026 na Macu Łukasza powstał klucz `~/.ssh/kancelaria`
i wpis `Host kancelaria` w `~/.ssh/config` – docelowo logowanie samym `ssh kancelaria`, bez hasła.
Klucz miał zostać wgrany przez tryb ratunkowy OVH: serwer zrestartowano w trybie Rescue, Łukasz dostał
komendę wgrywającą klucz. **Nie mam potwierdzenia, że klucz wgrano i że serwer wrócił do normalnego
startu (Boot = LOCAL).** Jeśli serwer nadal stoi w trybie Rescue, Monika nie działa. Pierwszy krok
w nowym projekcie: sprawdzić w panelu OVH tryb startu i `ssh kancelaria`.

## 4. Jak się rozwija

- Zmiany wyłącznie przez gałąź i PR; Claude scala sam po zielonych testach (GitHub Actions + lokalnie
  `.venv/bin/pytest -q`) i informuje jednym zdaniem. Serwer sam pobiera `main` co 5 min.
- Claude nie ma dostępu do serwera z sesji – komendy na serwerze wkleja Łukasz (`ssh kancelaria`).
- Diagnoza: `journalctl -u monika -n 60 --no-pager` oraz
  `grep -E 'task_failed|agent_error|action_failed|mail_blocked' ~/kancelaria-agent/logs/audit.jsonl | tail -n 15`.
- Tajemnica adwokacka: żadnych danych spraw w repo, testach, PR, issue – tylko dane fikcyjne.

## 5. Otwarte sprawy (kolejność)

1. **Dostęp SSH + powrót serwera z trybu Rescue** (pkt 3).
2. **Monika nie odpowiada na zlecenia.** Odpisała tylko na „diagnostykę” (ścieżka bez modelu AI); na
   zwykłe zlecenia – cisza. Przyczyna nieznana – potrzebny log z komend z pkt 4. Hipoteza
   (niesprawdzona): klucz Anthropic wklejony do `.env` zaczynał się od `sk-ant-usr-`, a klucze API mają
   zwykle postać `sk-ant-api03-`. Uwaga: błąd modelu powinien skutkować mailem „przerwało mi pracę” –
   skoro nie przyszedł nawet ten, problem może być wcześniej (np. zadanie wisi w stanie „running”, brak
   instalacji `install.sh`, mail nieuznany za zaufany). Sprawdzić też, czy `./deploy/install.sh` był
   uruchomiony (timer aktualizacji).
3. **Baza wiedzy Moniki** – folder `data/wiedza/` na VPS, który Monika przeszukuje i czyta przy każdym
   zleceniu; wrzucanie plików mailem (temat „wiedza”) oraz przez **panel WWW** (proponowany adres
   monika.kancelariabil.pl – wymaga rekordu DNS A `monika` → 54.39.231.254 w MyDevil). Forma logowania do
   panelu – do decyzji Łukasza (zaproponowano „Zaloguj przez Google” dla adresów z `team`; dokumenty są
   objęte tajemnicą adwokacką). Nic jeszcze nie zbudowano.
4. **System kontroli pracy Moniki** (podgląd spraw, historii, kosztów, wyłącznik) – rozwinięcie panelu z pkt 3.
5. Ulepszenia na podstawie pierwszych prawdziwych zleceń (`prompts/doswiadczenia.md`).
6. Dalsze etapy z `docs/plan.md` (organizacja skrzynki Basi, inFakt, pisma porównawcze, treści spolkazoo).

## 6. Przekazanie kancelarii (gdy Łukasz o to poprosi)

- Nowy klucz API Anthropic i nowy klucz klienta Google (obecne były wklejone w rozmowie) – podmiana w `.env`.
- Usunięcie lukasz@brandwise.pl z `team` i `allowed_recipients`; usunięcie Lukasz904 z repo.
- Usunięcie klucza SSH Łukasza z serwera; hasło użytkownika `ubuntu` znane tylko kancelarii.
- DNS kancelariabil.pl (i strona) z konta MyDevil Brandwise na konto kancelarii.
- Ustalenie osoby zatwierdzającej zmiany (z kim Monika koresponduje) po stronie kancelarii.
- Adresy Anety i Agnieszki w `team`; import wzorów dokumentów.
- Instrukcja projektu Claude dla zespołu: `docs/instrukcja-projektu-claude.md`.
