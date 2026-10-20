# Stan wdrożenia Moniki

Aktualizuj ten plik przy każdej istotnej zmianie (PR). Ostatnia aktualizacja: 20.10.2026.

## Działa
- Serwer: OVH VPS-1 (2 vCore, 4 GB RAM), IP 54.39.231.254, Ubuntu 24.04, użytkownik `ubuntu`,
  katalog `~/kancelaria-agent`, usługa systemd `monika`, aktualizacja z `main` co 5 min (`monika-update.timer`).
- Repo: GitHub `KancelariaBil/kancelaria-agent` (prywatne). Serwer pobiera kod kluczem tylko do odczytu
  (Deploy key „serwer”). Aplikacja Claude na GitHubie zainstalowana dla tego repo.
- Skrzynka Moniki: biuro@kancelariabil.pl (Google Workspace kancelarii, tylko dla Moniki – nie podawać
  klientom). Logowanie OAuth: projekt Google Cloud „Monika poczta” (monika-poczta), ekran zgody
  wewnętrzny, klient OAuth `10144385402-a9u0sn4aahsq6imlff79us9cccq06ddi.apps.googleusercontent.com`.
- Zespół (`team` w config.yaml): na razie tylko barbara.bil@kancelariabil.pl. Monika pisze tylko do Basi.
- Tryb próbny wyłączony 20.10.2026 (decyzja Łukasza) – odpowiedzi wychodzą naprawdę, tylko do Basi.
- DNS kancelariabil.pl: dodane rekordy `google._domainkey` (DKIM Google) i `_dmarc` (`v=DMARC1; p=none`).

## Do zrobienia przy przekazaniu (odcięcie od Brandwise / Łukasza)
- [ ] admin.google.com → Uwierzytelnianie e-maili → „Rozpocznij uwierzytelnianie” (DKIM) – bez tego
      Monika nie uznaje maili zespołu (sprawdź: mail do Moniki o temacie „diagnostyka” od Basi).
- [ ] Nowy klucz API Anthropic i nowy klucz klienta Google (obecne były wklejone w rozmowie z Łukaszem)
      – podmiana w `~/kancelaria-agent/.env` na serwerze, potem `sudo systemctl restart monika`.
- [ ] Usunięcie Lukasz904 z Collaborators repo.
- [ ] Hasło użytkownika `ubuntu` na serwerze – zmiana na znane tylko kancelarii (`passwd`).
- [ ] DNS kancelariabil.pl i strona kancelarii są na koncie MyDevil Brandwise (użytkownik „brand”,
      panel23.mydevil.net) – przenieść domenę na konto kancelarii.
- [ ] Konto OVH serwera – sprawdzić, że jest na kancelarię.

## Otwarte decyzje
- Kto zatwierdza zmiany tego, z kim Monika koresponduje (do czasu decyzji: Basia i Łukasz).
- Adresy Anety i Agnieszki – dopisać do `team` i `allowed_recipients` (PR, za zgodą jw.).
- Wzory dokumentów: ZIP z Worda → `wzory-import` (docs/instalacja.md, krok 5).

## Następne kroki rozwoju (kolejność ustalona z Łukaszem)
1. System kontroli pracy Moniki (podgląd spraw, historii, kosztów, wyłącznik) – wzorem panelu Marka
   z Brandwise, ale osobny kod w tym repo.
2. Ulepszenia na podstawie pierwszych prawdziwych zleceń (lekcje do `prompts/doswiadczenia.md`).
3. Dalsze etapy z `docs/plan.md`.
