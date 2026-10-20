"""Kalkulator odsetek (deterministyczny – model nigdy nie liczy odsetek sam).

Rodzaje (stan prawny od 1.01.2016):
- "kapitalowe"  – odsetki ustawowe, art. 359 § 2 k.c.: stopa referencyjna NBP + 3,5 pp,
- "opoznienie"  – odsetki ustawowe za opóźnienie, art. 481 § 2 k.c.: stopa referencyjna + 5,5 pp,
- "handlowe"    – za opóźnienie w transakcjach handlowych, art. 4 pkt 3 ustawy o przeciwdziałaniu
  nadmiernym opóźnieniom w transakcjach handlowych: stopa referencyjna obowiązująca 1 stycznia
  (dla I półrocza) albo 1 lipca (dla II półrocza) + 10 pp (od 1.01.2020; wcześniej i dla dłużnika
  będącego publicznym podmiotem leczniczym + 8 pp).
Zmienne stopy w kodeksie cywilnym zmieniają się w dniu zmiany stopy referencyjnej.

Liczenie: odsetki proste, dni okresu włącznie z oboma krańcami, rok = 365 dni (założenie podawane
w wyniku). Kwoty w groszach, zaokrąglenie połówkowe w górę dla każdego odcinka.

Tabela stóp: STOPA_REFERENCYJNA – dane, nie logika. Aktualizacja wyłącznie przez PR po sprawdzeniu
w komunikacie NBP; SPRAWDZONO = dzień ostatniego sprawdzenia (dla późniejszych dni wynik dostaje
ostrzeżenie, że stopa mogła się zmienić).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

# (obowiązuje od, stopa referencyjna NBP w %). Źródła: komunikaty RPP/NBP; zestawienie sprawdzone
# 2026-10-20 (m.in. taxmachine.pl/narzedzia/stopy-nbp, ksiegowosc.infor.pl, bankowynet.pl).
STOPA_REFERENCYJNA: tuple[tuple[date, Decimal], ...] = (
    (date(2015, 3, 5), Decimal("1.50")),
    (date(2020, 3, 18), Decimal("1.00")),
    (date(2020, 4, 9), Decimal("0.50")),
    (date(2020, 5, 29), Decimal("0.10")),
    (date(2021, 10, 7), Decimal("0.50")),
    (date(2021, 11, 4), Decimal("1.25")),
    (date(2021, 12, 9), Decimal("1.75")),
    (date(2022, 1, 5), Decimal("2.25")),
    (date(2022, 2, 9), Decimal("2.75")),
    (date(2022, 3, 9), Decimal("3.50")),
    (date(2022, 4, 7), Decimal("4.50")),
    (date(2022, 5, 6), Decimal("5.25")),
    (date(2022, 6, 9), Decimal("6.00")),
    (date(2022, 7, 8), Decimal("6.50")),
    (date(2022, 9, 8), Decimal("6.75")),
    (date(2023, 9, 7), Decimal("6.00")),
    (date(2023, 10, 5), Decimal("5.75")),
    (date(2025, 5, 8), Decimal("5.25")),
    (date(2025, 7, 3), Decimal("5.00")),
    (date(2025, 9, 4), Decimal("4.75")),
    (date(2025, 10, 9), Decimal("4.50")),
    (date(2025, 11, 6), Decimal("4.25")),
    (date(2025, 12, 4), Decimal("4.00")),
    (date(2026, 3, 5), Decimal("3.75")),
)
SPRAWDZONO = date(2026, 10, 20)
POCZATEK = date(2016, 1, 1)          # przed tą datą inne przepisy (odsetki ustawowe z rozporządzenia RM)
MARZA = {"kapitalowe": Decimal("3.5"), "opoznienie": Decimal("5.5")}
DNI_W_ROKU = Decimal(365)
RODZAJE = ("kapitalowe", "opoznienie", "handlowe")
NAZWY = {"kapitalowe": "odsetki ustawowe (art. 359 § 2 k.c.)",
         "opoznienie": "odsetki ustawowe za opóźnienie (art. 481 § 2 k.c.)",
         "handlowe": "odsetki za opóźnienie w transakcjach handlowych (art. 4 pkt 3 ustawy o przeciwdziałaniu "
                     "nadmiernym opóźnieniom w transakcjach handlowych)"}


class OdsetkiError(ValueError):
    pass


@dataclass(frozen=True)
class Odcinek:
    od: date
    do: date
    stopa: Decimal     # % w skali roku
    dni: int
    kwota: Decimal


@dataclass(frozen=True)
class Wynik:
    rodzaj: str
    kapital: Decimal
    odcinki: tuple[Odcinek, ...]
    ostrzezenia: tuple[str, ...]

    @property
    def suma(self) -> Decimal:
        return sum((o.kwota for o in self.odcinki), Decimal("0.00"))

    def opis(self) -> str:
        lines = [f"{NAZWY[self.rodzaj]} od kwoty {zl(self.kapital)}:"]
        for o in self.odcinki:
            lines.append(f"- {o.od:%d.%m.%Y}–{o.do:%d.%m.%Y}: {o.dni} dni × {fmt_pct(o.stopa)} = {zl(o.kwota)}")
        lines.append(f"Razem odsetki: {zl(self.suma)} (dni liczone włącznie z obiema datami, rok = 365 dni)")
        lines += [f"UWAGA: {w}" for w in self.ostrzezenia]
        return "\n".join(lines)


def zl(v: Decimal) -> str:
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} zł"


def fmt_pct(v: Decimal) -> str:
    return f"{v.normalize():f}".replace(".", ",") + "%"


def stopa_referencyjna(dzien: date) -> Decimal:
    if dzien < STOPA_REFERENCYJNA[0][0]:
        raise OdsetkiError(f"brak stopy referencyjnej w tabeli dla {dzien:%d.%m.%Y}")
    value = STOPA_REFERENCYJNA[0][1]
    for since, rate in STOPA_REFERENCYJNA:
        if since <= dzien:
            value = rate
        else:
            break
    return value


def stopa(rodzaj: str, dzien: date, podmiot_leczniczy: bool = False,
          transakcja_przed_2020: bool = False) -> Decimal:
    if rodzaj in MARZA:
        return stopa_referencyjna(dzien) + MARZA[rodzaj]
    if rodzaj == "handlowe":
        polrocze = date(dzien.year, 1, 1) if dzien.month <= 6 else date(dzien.year, 7, 1)
        plus = Decimal("8") if (podmiot_leczniczy or transakcja_przed_2020 or dzien.year < 2020) else Decimal("10")
        return stopa_referencyjna(polrocze) + plus
    raise OdsetkiError(f"nieznany rodzaj odsetek: {rodzaj} (dozwolone: {', '.join(RODZAJE)})")


def _granice(rodzaj: str, od: date, do: date) -> list[date]:
    """Dni, od których zmienia się stopa (w obrębie okresu)."""
    out = set()
    if rodzaj in MARZA:
        out |= {d for d, _ in STOPA_REFERENCYJNA if od < d <= do}
    else:
        for y in range(od.year, do.year + 1):
            out |= {d for d in (date(y, 1, 1), date(y, 7, 1)) if od < d <= do}
    return sorted(out)


def oblicz(kapital, od: date, do: date, rodzaj: str = "opoznienie", podmiot_leczniczy: bool = False,
           transakcja_przed_2020: bool = False) -> Wynik:
    """Odsetki od `kapital` za dni od `od` do `do` włącznie (pierwszy dzień opóźnienia – dzień zapłaty)."""
    try:
        kapital = Decimal(str(kapital).replace(" ", "").replace(",", ".")).quantize(Decimal("0.01"))
    except Exception:
        raise OdsetkiError("niepoprawna kwota") from None
    if kapital <= 0 or kapital > Decimal("1000000000"):
        raise OdsetkiError("kwota musi być dodatnia (do 1 mld zł)")
    if rodzaj not in RODZAJE:
        raise OdsetkiError(f"nieznany rodzaj odsetek: {rodzaj} (dozwolone: {', '.join(RODZAJE)})")
    if do < od:
        raise OdsetkiError("data końcowa przed początkową")
    if od < POCZATEK:
        raise OdsetkiError("okres przed 1.01.2016 – inne przepisy (rozporządzenie RM); policz ręcznie")
    if (do - od).days > 366 * 30:
        raise OdsetkiError("okres dłuższy niż 30 lat")
    ostrz = []
    if do > SPRAWDZONO:
        ostrz.append(f"tabela stóp sprawdzona do {SPRAWDZONO:%d.%m.%Y}; dla dni po tej dacie przyjęto ostatnią "
                     "znaną stopę – sprawdź, czy RPP jej nie zmieniła")
    if rodzaj == "handlowe" and od.year < 2020 and not transakcja_przed_2020:
        ostrz.append("okres przed 2020: przyjęto +8 pp; dla transakcji zawartych przed 1.01.2020 stawka +8 pp "
                     "obowiązuje także później (ustaw transakcja_przed_2020)")
    odcinki = []
    start = od
    for g in [*_granice(rodzaj, od, do), do + timedelta(days=1)]:
        end = g - timedelta(days=1)
        if end < start:
            continue
        s = stopa(rodzaj, start, podmiot_leczniczy, transakcja_przed_2020)
        dni = (end - start).days + 1
        kwota = (kapital * s / Decimal(100) * Decimal(dni) / DNI_W_ROKU).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if odcinki and odcinki[-1].stopa == s:   # ta sama stopa po obu stronach granicy – łączymy
            prev = odcinki.pop()
            dni += prev.dni
            start = prev.od
            kwota = (kapital * s / Decimal(100) * Decimal(dni) / DNI_W_ROKU).quantize(Decimal("0.01"), ROUND_HALF_UP)
        odcinki.append(Odcinek(start, end, s, dni, kwota))
        start = g
    return Wynik(rodzaj, kapital, tuple(odcinki), tuple(ostrz))


def parse_date(value: str) -> date:
    s = str(value).strip()
    for sep in (".", "-", "/"):
        parts = s.split(sep)
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            a, b, c = (int(p) for p in parts)
            try:
                return date(a, b, c) if len(parts[0]) == 4 else date(c, b, a)
            except ValueError:
                break
    raise OdsetkiError(f"niepoprawna data: {value!r} (format dd.mm.rrrr albo rrrr-mm-dd)")
