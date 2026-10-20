# Instalacja Moniki na serwerze kancelarii

Serwer: OVH VPS, Ubuntu 24.04, IP 54.39.231.254, użytkownik `ubuntu`.
Komendy wkleja człowiek. „Mac” = Terminal na Macu, „serwer” = terminal po zalogowaniu `ssh ubuntu@54.39.231.254`.

## 1. Klucz do repozytorium (serwer)
```
ssh-keygen -t ed25519 -N "" -f ~/.ssh/kancelaria_deploy -C monika-serwer
cat ~/.ssh/kancelaria_deploy.pub
```
Wyświetlony klucz dodaj w GitHubie: repo KancelariaBil/kancelaria-agent → Settings → Deploy keys → Add
(tylko odczyt, bez „Allow write access”). Potem na serwerze:
```
printf 'Host github.com\n  IdentityFile ~/.ssh/kancelaria_deploy\n' >> ~/.ssh/config
git clone git@github.com:KancelariaBil/kancelaria-agent.git ~/kancelaria-agent
```

## 2. Python i biblioteki (serwer)
```
sudo apt-get update && sudo apt-get install -y python3-venv git
cd ~/kancelaria-agent && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -e .
cp .env.example .env && chmod 600 .env && nano .env
```
W `.env` wpisz: `ANTHROPIC_API_KEY` (konto kancelarii), `AGENT_EMAIL=biuro@kancelariabil.pl`,
`GMAIL_CLIENT_ID` i `GMAIL_CLIENT_SECRET` (krok 3). Zapisz: Ctrl+O, Enter, Ctrl+X.

## 3. Dostęp do skrzynki (Google)
W Google Cloud na koncie kancelarii: nowy projekt → włącz Gmail API → ekran zgody „Wewnętrzny” →
Dane logowania → Identyfikator klienta OAuth, typ „Aplikacja internetowa”, URI przekierowania
`http://localhost:8765/callback`. Identyfikator i klucz klienta wpisz do `.env` (krok 2).

Autoryzacja – na Macu otwórz tunel (zostaw okno otwarte):
```
ssh -L 8765:localhost:8765 ubuntu@54.39.231.254
```
w tym oknie (to już serwer):
```
cd ~/kancelaria-agent && .venv/bin/python -m kancelaria_agent oauth-setup
```
otwórz na Macu http://localhost:8765 i zaloguj się jako skrzynka Moniki. Potem test:
```
.venv/bin/python -m kancelaria_agent mailcheck
```

## 4. Usługa i automatyczne aktualizacje (serwer)
```
cd ~/kancelaria-agent && ./deploy/install.sh
```
Od tej chwili serwer co 5 minut pobiera zatwierdzone zmiany z `main` i restartuje Monikę.

## 5. Wzory dokumentów (Mac → serwer)
Na Macu, w katalogu z plikiem ZIP:
```
scp wzory.zip ubuntu@54.39.231.254:~/
```
na serwerze:
```
cd ~/kancelaria-agent && .venv/bin/python -m kancelaria_agent wzory-import ~/wzory.zip && rm ~/wzory.zip
```

## Na co dzień
- Zatrzymanie: `touch ~/kancelaria-agent/STOP` (wznowienie: `rm ~/kancelaria-agent/STOP`).
- Propozycje w trybie próbnym: `.venv/bin/python -m kancelaria_agent proposals`,
  zatwierdzenie `... approve <numer>`.
- Stan: mail do Moniki o temacie „diagnostyka”.
