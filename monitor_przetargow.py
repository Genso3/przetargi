#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_przetargow.py
=====================
Prosty monitoring przetargów publicznych z BZP (Platforma e-Zamowienia)
pod katem firmy dostarczajacej materialy dachowe i systemy asekuracyjne.

Logika (zgodna z omawianym pipeline'em):
  1. Pobierz swieze ogloszenia z API BZP.
  2. Odfiltruj po kodach CPV + slowach kluczowych.
  3. Rozdziel: ogloszenia O ZAMOWIENIU (lead wczesny) vs O WYNIKU (zwyciezca + NIP).
  4. Pomin to, co juz widziales (plik seen_ids.json).
  5. Powiadom (na start: wydruk + zapis do CSV; opcjonalnie e-mail).

WAZNE - przeczytaj przed pierwszym uruchomieniem:
  - API BZP jest BEZPLATNE i nie wymaga rejestracji do ODCZYTU ogloszen krajowych.
  - Endpoint bazowy: https://ezamowienia.gov.pl/mo-board/api/v1/notice
  - Dokladne nazwy parametrow zapytania (daty, CPV, paginacja) sa opisane w
    oficjalnej "Instrukcji API BZP (Zalacznik nr 3)" na Portalu Deweloperskim:
    https://ezamowienia.gov.pl/pl/integracja/
  - Dlatego skrypt przy pierwszym uruchomieniu WYDRUKUJE surowa strukture
    odpowiedzi (funkcja inspect_api). Dopasujesz wtedy nazwy pol w sekcji
    oznaczonej # >>> DOPASUJ <<< i gotowe.
"""

import json
import csv
import os
import sys
import smtplib
from datetime import date, timedelta
from email.mime.text import MIMEText

import requests  # pip install requests


# ============================================================
# 1. KONFIGURACJA - to jest jedyna czesc, ktora edytujesz na co dzien
# ============================================================

# Kody CPV pod profil CW Lundberg (parasole + konkretne).
# Pierwsze 4-5 znakow lapie cala galaz (np. "45261" = wszystkie pokrycia dachowe).
KODY_CPV = [
    "45261",    # wykonywanie pokryc i konstrukcji dachowych (dachy skosne, krycie)
    "45261420", # uszczelnianie dachow (membrany - dachy wielkopowierzchniowe/plaskie)
    "45261900", # naprawa i konserwacja dachow
    "45340000", # instalowanie ogrodzen, plotow i sprzetu ochronnego
    "34928300", # bariery ochronne
    "44112410", # konstrukcje dachowe (wyrob)
    "09331",    # baterie sloneczne / PV (jesli celujesz w montaz pod fotowoltaike)
]

# Slowa kluczowe - drugie sito, niezalezne od CPV.
# Lapia przetargi opisane kodem ogolnym (45000000 "roboty budowlane"),
# w ktorych dach to tylko fragment. Szukane w tytule i przedmiocie zamowienia.
SLOWA_KLUCZOWE = [
    "dach", "pokrycie dachow", "membrana", "blach", "papa",
    "asekuracj", "punkt kotwicz", "zabezpieczenie przed upadkiem",
    "lina asekuracyjn", "lawy kominiar", "stopnie kominiar",
    "bariera sniegow", "komunikacja dachow", "hala", "magazyn",
]

# Ile dni wstecz pobierac przy kazdym uruchomieniu (z zapasem na weekend).
DNI_WSTECZ = 2

# Plik pamieci - tu trzymamy ID ogloszen juz pokazanych.
PLIK_PAMIECI = "seen_ids.json"
PLIK_WYNIKOW = "nowe_przetargi.csv"

# E-mail (opcjonalnie). Zostaw WYSYLAJ_EMAIL = False zeby tylko zapisywac do CSV.
WYSYLAJ_EMAIL = False
EMAIL_FROM = "twoj@gmail.com"
EMAIL_TO = "twoj@gmail.com"
EMAIL_HASLO = os.environ.get("EMAIL_HASLO", "")  # haslo aplikacji, NIE w kodzie
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# Endpoint API BZP
API_BASE = "https://ezamowienia.gov.pl/mo-board/api/v1"


# ============================================================
# 2. POMOCNICZE: pamiec (zeby nie powiadamiac dwa razy)
# ============================================================

def wczytaj_pamiec():
    if os.path.exists(PLIK_PAMIECI):
        with open(PLIK_PAMIECI, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def zapisz_pamiec(ids):
    with open(PLIK_PAMIECI, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


# ============================================================
# 3. POBIERANIE Z API
# ============================================================

def inspect_api():
    """
    URUCHOM TO RAZ, recznie: python monitor_przetargow.py --inspect
    Wydrukuje surowa odpowiedz API, zebys zobaczyl, jak nazywaja sie pola
    (tytul, CPV, data, typ ogloszenia, sekcja ze zwyciezca).
    """
    print(">>> Odpytuje API i drukuje pierwsza odpowiedz...\n")
    r = requests.get(f"{API_BASE}/notice", params={"PageNumber": 1, "PageSize": 2},
                     timeout=30)
    print("Status:", r.status_code)
    print("Naglowki:", dict(r.headers).get("Content-Type"))
    try:
        data = r.json()
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
    except Exception:
        print(r.text[:4000])


# Typ ogloszenia potwierdzony jako dzialajacy w tym API.
# (Ogloszenia o WYNIKU maja inna nazwe typu - jeszcze jej szukamy.)
NOTICE_TYPES = ["ContractNotice"]


def pobierz_ogloszenia():
    """
    DZIALAJACA wersja. API wymaga 3 obowiazkowych parametrow naraz:
    NoticeType + PublicationDateFrom + PublicationDateTo (potwierdzone w logu).
    Pobiera osobno kazdy typ ogloszenia i skleja wyniki.
    Przy pierwszym ogloszeniu wypisuje jego strukture (nazwy pol),
    zebysmy mogli poprawnie podpiac tytul, CPV, zwyciezce itd.
    """
    od = (date.today() - timedelta(days=DNI_WSTECZ)).isoformat()
    do = date.today().isoformat()
    wszystkie = []

    for nt in NOTICE_TYPES:
        strona = 1
        while True:
            params = {
                "NoticeType": nt,
                "PublicationDateFrom": od,
                "PublicationDateTo": do,
                "PageSize": 100,
                "PageNumber": strona,
            }
            r = requests.get(f"{API_BASE}/notice", params=params, timeout=30)
            if r.status_code != 200:
                # niepoprawny typ ogloszenia lub inny blad - pomijamy, ale logujemy
                print(f"  [{nt}] pominiety (status {r.status_code}): {r.text[:200]}")
                break
            dane = r.json()
            pozycje = dane if isinstance(dane, list) else (
                dane.get("value") or dane.get("items") or dane.get("data")
                or dane.get("content") or [])

            if not pozycje:
                break
            wszystkie.extend(pozycje)
            print(f"  [{nt}] strona {strona}: +{len(pozycje)} (razem {len(wszystkie)})")
            if len(pozycje) < 100:
                break
            strona += 1
            if strona > 50:   # bezpiecznik
                break

    return wszystkie


# ============================================================
# 4. FILTROWANIE
# ============================================================

def _kody_cpv(ogloszenie):
    """Wyciaga liste samych kodow CPV z pola cpvCode.
    Format API: '71520000-9 (Uslugi nadzoru),71247000-1 (Nadzor...)'.
    Zwraca np. ['71520000-9', '71247000-1']."""
    surowe = str(ogloszenie.get("cpvCode") or "")
    kody = []
    for czesc in surowe.split(","):
        czesc = czesc.strip()
        if not czesc:
            continue
        kod = czesc.split(" ")[0].split("(")[0].strip()  # tylko kod, bez opisu
        if kod:
            kody.append(kod)
    return kody


def pasuje_cpv(ogloszenie):
    """Sprawdza, czy ktorykolwiek z kodow CPV ogloszenia zaczyna sie
    od ktoregos z Twoich kodow (KODY_CPV)."""
    kody = _kody_cpv(ogloszenie)
    return any(kod.startswith(k) for kod in kody for k in KODY_CPV)


def pasuje_slowa(ogloszenie):
    tekst = str(ogloszenie.get("orderObject", "")).lower()
    return any(slowo in tekst for slowo in SLOWA_KLUCZOWE)


def typ_ogloszenia(ogloszenie):
    """ContractNotice = ogloszenie O ZAMOWIENIU (nowy przetarg).
    Jesli pojawi sie wynik (procedureResult) lub typ zawiera award/result
    -> traktujemy jako WYNIK (zwyciezca)."""
    typ = str(ogloszenie.get("noticeType") or "").lower()
    if ogloszenie.get("procedureResult") or "award" in typ or "result" in typ \
            or "wynik" in typ or "udziel" in typ:
        return "WYNIK"
    return "ZAMOWIENIE"


def wyciagnij_zwyciezce(ogloszenie):
    """Probuje wyciagnac zwyciezce z procedureResult (gdy ogloszenie o wyniku).
    Struktura tego pola bedzie znana dopiero, gdy ustalimy typ ogloszen o wyniku."""
    nazwa, nip = "", ""
    pr = ogloszenie.get("procedureResult")
    if isinstance(pr, dict):
        nazwa = pr.get("contractorName") or pr.get("name") or ""
        nip = pr.get("contractorNationalId") or pr.get("nip") or ""
    elif isinstance(pr, list) and pr:
        w = pr[0]
        if isinstance(w, dict):
            nazwa = w.get("contractorName") or w.get("name") or ""
            nip = w.get("contractorNationalId") or w.get("nip") or ""
    return nazwa, nip


# ============================================================
# 5. POWIADOMIENIE
# ============================================================

def link_ogloszenia(ogloszenie):
    """DZIALAJACY adres strony postepowania w przegladarce.
    Zweryfikowany format e-Zamowienia (uzywa identyfikatora OCDS tenderId):
      https://ezamowienia.gov.pl/mp-client/search/list/{tenderId}
    Tam sa szczegoly postepowania, SWZ i zalaczniki."""
    tid = ogloszenie.get("tenderId") or ""
    if tid:
        return f"https://ezamowienia.gov.pl/mp-client/search/list/{tid}"
    return "https://ezamowienia.gov.pl/mo-client-board/bzp/list"


link_pdf = link_ogloszenia  # alias dla zgodnosci


def _pierwszy_wiersz(tekst, limit=130):
    """Tytul z orderObject: pierwsza linia, przycieta."""
    t = str(tekst or "").strip().split("\n")[0].strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def wyciagnij_szczegoly(o):
    """Mapuje POTWIERDZONE pola z API na informacje do karty."""
    miasto = o.get("organizationCity") or ""
    return {
        "zamawiajacy": o.get("organizationName") or "",
        "lokalizacja": miasto,
        "wartosc": "",   # API nie podaje wartosci w tym ogloszeniu
        "termin": str(o.get("submittingOffersDate") or "")[:16].replace("T", " "),
        "opis": str(o.get("orderObject") or "")[:600],
        "cpv_kody": ", ".join(_kody_cpv(o)[:4]),  # do 4 kodow dla czytelnosci
        "tryb": o.get("noticeType") or "",
    }


def zapisz_csv(nowe):
    nowy_plik = not os.path.exists(PLIK_WYNIKOW)
    with open(PLIK_WYNIKOW, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if nowy_plik:
            w.writerow(["data", "typ", "tytul", "zwyciezca", "nip", "link_pdf"])
        for o in nowe:
            t = typ_ogloszenia(o)
            naz, nip = wyciagnij_zwyciezce(o) if t == "WYNIK" else ("", "")
            w.writerow([
                date.today().isoformat(), t,
                _pierwszy_wiersz(o.get("orderObject"), 200), naz, nip, link_pdf(o),
            ])


def zapisz_json(wszystkie_dopasowane):
    """Zapisuje PELNA aktualna liste dopasowanych ogloszen do dane.json,
    ktory czyta strona internetowa. Nadpisuje plik za kazdym razem
    (strona zawsze pokazuje aktualny stan, nie tylko nowosci)."""
    rekordy = []
    for o in wszystkie_dopasowane:
        t = typ_ogloszenia(o)
        naz, nip = wyciagnij_zwyciezce(o) if t == "WYNIK" else ("", "")
        szcz = wyciagnij_szczegoly(o)
        rekordy.append({
            "data": str(o.get("publicationDate") or date.today().isoformat())[:10],
            "typ": t,
            "tytul": _pierwszy_wiersz(o.get("orderObject")),
            "zwyciezca": naz,
            "nip": nip,
            "cpv": szcz["cpv_kody"],
            "zamawiajacy": szcz["zamawiajacy"],
            "lokalizacja": szcz["lokalizacja"],
            "wartosc": szcz["wartosc"],
            "termin": szcz["termin"],
            "opis": szcz["opis"],
            "tryb": szcz["tryb"],
            "link": link_ogloszenia(o),
            "bzp": o.get("bzpNumber") or "",
        })
    # najnowsze na gorze
    rekordy.sort(key=lambda r: r["data"], reverse=True)
    with open("dane.json", "w", encoding="utf-8") as f:
        json.dump({"aktualizacja": date.today().isoformat(),
                   "ogloszenia": rekordy}, f, ensure_ascii=False, indent=2)
    print(f"  Zapisano dane.json ({len(rekordy)} ogloszen) dla strony.")


def wyslij_email(nowe):
    if not nowe:
        return
    linie = []
    for o in nowe:
        t = typ_ogloszenia(o)
        naglowek = "[WYNIK]" if t == "WYNIK" else "[NOWY PRZETARG]"
        linia = f"{naglowek} {_pierwszy_wiersz(o.get('orderObject'))}"
        if t == "WYNIK":
            naz, nip = wyciagnij_zwyciezce(o)
            linia += f"\n   ZWYCIEZCA: {naz} (NIP {nip})"
        linia += f"\n   {link_pdf(o)}"
        linie.append(linia)

    tresc = f"Znaleziono {len(nowe)} nowych ogloszen:\n\n" + "\n\n".join(linie)
    msg = MIMEText(tresc, "plain", "utf-8")
    msg["Subject"] = f"Przetargi: {len(nowe)} nowych ({date.today().isoformat()})"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
        s.starttls()
        s.login(EMAIL_FROM, EMAIL_HASLO)
        s.send_message(msg)
    print("E-mail wyslany.")


# ============================================================
# 6. GLOWNY PRZEBIEG
# ============================================================

def main():
    if "--inspect" in sys.argv:
        inspect_api()
        return

    print(f"[{date.today()}] Pobieram ogloszenia z ostatnich {DNI_WSTECZ} dni...")
    ogloszenia = pobierz_ogloszenia()
    print(f"  Pobrano: {len(ogloszenia)}")

    # Filtr: CPV LUB slowa kluczowe (suma, zeby nic nie ucieklo)
    dopasowane = [o for o in ogloszenia if pasuje_cpv(o) or pasuje_slowa(o)]
    print(f"  Po filtrze CPV/slowa: {len(dopasowane)}")

    # Pamiec - tylko nowe
    widziane = wczytaj_pamiec()
    nowe = []
    for o in dopasowane:
        oid = str(o.get("bzpNumber") or o.get("noticeNumber") or o.get("tenderId") or "")
        if oid not in widziane:
            nowe.append(o)
            widziane.add(oid)

    print(f"  Nowych (niewidzianych wczesniej): {len(nowe)}")

    if nowe:
        zapisz_csv(nowe)
        if WYSYLAJ_EMAIL:
            wyslij_email(nowe)
        for o in nowe:
            t = typ_ogloszenia(o)
            tag = "WYNIK " if t == "WYNIK" else "PRZETARG"
            print(f"   [{tag}] {_pierwszy_wiersz(o.get('orderObject'), 90)}")
            if t == "WYNIK":
                naz, nip = wyciagnij_zwyciezce(o)
                print(f"            -> zwyciezca: {naz} (NIP {nip})")

    # Zapis dla STRONY: pelna aktualna lista dopasowanych (nie tylko nowosci)
    zapisz_json(dopasowane)

    zapisz_pamiec(widziane)
    print("Gotowe.")


if __name__ == "__main__":
    main()
