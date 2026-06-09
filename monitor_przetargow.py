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


def pobierz_ogloszenia():
    """
    Pobiera ogloszenia. WERSJA DIAGNOSTYCZNA: probuje kilku wariantow
    formatu zapytania i przy kazdym wypisuje status oraz POCZATEK
    odpowiedzi serwera. Dzieki temu w logu GitHub Actions zobaczymy,
    ktory format dziala (status 200) i jak nazywaja sie pola.
    """
    od = (date.today() - timedelta(days=DNI_WSTECZ)).isoformat()
    do = date.today().isoformat()

    # Lista wariantow do przetestowania (endpoint, parametry)
    warianty = [
        (f"{API_BASE}/notice", {"PublicationDateFrom": od, "PageNumber": 1, "PageSize": 20}),
        (f"{API_BASE}/notice", {"publicationDateFrom": od, "pageNumber": 1, "pageSize": 20}),
        (f"{API_BASE}/notice", {"PublicationDateFrom": od, "PublicationDateTo": do, "PageSize": 20}),
        (f"{API_BASE}/notice", {"NoticeType": "ContractNotice", "PageSize": 20}),
        (f"{API_BASE}/notice", {"PageSize": 20, "PageNumber": 1}),
        (f"{API_BASE}/notice/search", {"PageSize": 20, "PageNumber": 1}),
        (f"{API_BASE}/notices", {"PageSize": 20, "PageNumber": 1}),
    ]

    print("=== DIAGNOSTYKA: testuje warianty zapytania do API ===")
    dzialajacy = None
    for i, (url, params) in enumerate(warianty, 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            print(f"\n[wariant {i}] {r.url}")
            print(f"  status: {r.status_code}  typ: {r.headers.get('Content-Type','?')[:40]}")
            tresc = r.text[:500].replace("\n", " ")
            print(f"  poczatek odpowiedzi: {tresc}")
            if r.status_code == 200 and "json" in r.headers.get("Content-Type", ""):
                print(f"  >>> WARIANT {i} DZIALA <<<")
                dzialajacy = (url, params)
                break
        except Exception as e:
            print(f"[wariant {i}] wyjatek: {e}")
    print("\n=== KONIEC DIAGNOSTYKI ===\n")

    if not dzialajacy:
        print("Zaden wariant nie zwrocil danych JSON. Skopiuj powyzszy log.")
        return []

    # Jesli ktorys zadzialal - pobieramy strona po stronie
    url, base_params = dzialajacy
    wszystkie = []
    strona = 1
    while True:
        p = dict(base_params)
        for k in list(p):
            if k.lower() == "pagenumber":
                p[k] = strona
        r = requests.get(url, params=p, timeout=30)
        if r.status_code != 200:
            break
        dane = r.json()
        pozycje = dane if isinstance(dane, list) else (
            dane.get("value") or dane.get("items") or dane.get("data")
            or dane.get("content") or [])
        if not pozycje:
            break
        wszystkie.extend(pozycje)
        if len(pozycje) < 20:
            break
        strona += 1
        if strona > 50:   # bezpiecznik
            break

    return wszystkie


# ============================================================
# 4. FILTROWANIE
# ============================================================

def pasuje_cpv(ogloszenie):
    # >>> DOPASUJ <<< nazwe pola z CPV. Czesto: "cpvCode", "mainCpv", "cpv".
    cpv = str(ogloszenie.get("cpvCode")
              or ogloszenie.get("mainCpv")
              or ogloszenie.get("cpv") or "")
    return any(cpv.startswith(k) for k in KODY_CPV)


def pasuje_slowa(ogloszenie):
    tekst = (str(ogloszenie.get("noticeTitle", ""))
             + " " + str(ogloszenie.get("subject", ""))
             + " " + str(ogloszenie.get("orderObject", ""))).lower()
    return any(slowo in tekst for slowo in SLOWA_KLUCZOWE)


def typ_ogloszenia(ogloszenie):
    """Rozroznia ogloszenie O ZAMOWIENIU od O WYNIKU.
    # >>> DOPASUJ <<< - typ bywa w polu "noticeType"/"orderType".
    Slowo 'wynik'/'udzielenie' -> ogloszenie o zwyciezcy."""
    typ = str(ogloszenie.get("noticeType")
              or ogloszenie.get("orderType") or "").lower()
    if "wynik" in typ or "udziel" in typ or "result" in typ:
        return "WYNIK"
    return "ZAMOWIENIE"


def wyciagnij_zwyciezce(ogloszenie):
    """Z ogloszenia o WYNIKU probuje wyciagnac nazwe i NIP zwyciezcy.
    # >>> DOPASUJ <<< - struktura zwyciezcy bywa zagniezdzona,
    np. ogloszenie["contractors"][0]["name"] / ["nip"].
    Dlatego najpierw zajrzyj do PDF ogloszenia (link nizej) albo inspect_api."""
    nazwa, nip = "", ""
    for klucz in ("contractors", "winners", "wykonawcy"):
        lista = ogloszenie.get(klucz)
        if isinstance(lista, list) and lista:
            w = lista[0]
            nazwa = w.get("name") or w.get("nazwa") or ""
            nip = w.get("nip") or w.get("taxId") or ""
            break
    return nazwa, nip


# ============================================================
# 5. POWIADOMIENIE
# ============================================================

def link_ogloszenia(ogloszenie):
    """Buduje DZIALAJACY adres strony ogloszenia w przegladarce.
    Zweryfikowany format e-Zamowienia:
      https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/{ID}
    # >>> DOPASUJ <<< - ID bywa pod 'objectId'/'noticeId'/'id'. inspect_api pokaze."""
    nid = (ogloszenie.get("objectId") or ogloszenie.get("noticeId")
           or ogloszenie.get("id") or "")
    if nid:
        return f"https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/{nid}"
    # awaryjnie: lista ogloszen (zawsze dziala)
    return "https://ezamowienia.gov.pl/mo-client-board/bzp/list"


# zachowujemy stara nazwe jako alias, zeby nie psuc reszty kodu
link_pdf = link_ogloszenia


def wyciagnij_szczegoly(o):
    """Wyciaga dodatkowe informacje do bogatszej karty.
    # >>> DOPASUJ <<< - nazwy pol potwierdzisz przez inspect_api.
    Funkcja jest 'odporna': probuje kilku typowych nazw i zwraca pusty string,
    gdy danego pola nie ma."""
    def pole(*nazwy):
        for n in nazwy:
            v = o.get(n)
            if v:
                return str(v)
        return ""
    return {
        "zamawiajacy": pole("organizationName", "contractingAuthority",
                            "zamawiajacy", "buyerName"),
        "lokalizacja": pole("voivodeship", "city", "place", "miejscowosc",
                            "deliveryPlace"),
        "wartosc": pole("orderValue", "estimatedValue", "wartosc", "value"),
        "termin": str(pole("submittingOffersDate", "offerDeadline",
                           "tenderSubmissionDeadline", "terminSkladania"))[:16],
        "opis": pole("orderObject", "subject", "shortDescription", "opis")[:600],
        "tryb": pole("procedureType", "tryb", "noticeType"),
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
                o.get("noticeTitle", "")[:200], naz, nip, link_pdf(o),
            ])


def zapisz_json(wszystkie_dopasowane):
    """Zapisuje PELNA aktualna liste dopasowanych ogloszen do dane.json,
    ktory czyta strona internetowa. Nadpisuje plik za kazdym razem
    (strona zawsze pokazuje aktualny stan, nie tylko nowosci)."""
    rekordy = []
    for o in wszystkie_dopasowane:
        t = typ_ogloszenia(o)
        naz, nip = wyciagnij_zwyciezce(o) if t == "WYNIK" else ("", "")
        cpv = str(o.get("cpvCode") or o.get("mainCpv") or o.get("cpv") or "")
        szcz = wyciagnij_szczegoly(o)
        rekordy.append({
            "data": str(o.get("publicationDate") or o.get("data") or date.today().isoformat())[:10],
            "typ": t,
            "tytul": o.get("noticeTitle", "") or o.get("subject", ""),
            "zwyciezca": naz,
            "nip": nip,
            "cpv": cpv,
            "zamawiajacy": szcz["zamawiajacy"],
            "lokalizacja": szcz["lokalizacja"],
            "wartosc": szcz["wartosc"],
            "termin": szcz["termin"],
            "opis": szcz["opis"],
            "tryb": szcz["tryb"],
            "link": link_ogloszenia(o),
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
        linia = f"{naglowek} {o.get('noticeTitle','')}"
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
        oid = str(o.get("noticeId") or o.get("id") or o.get("noticeTitle"))
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
            print(f"   [{tag}] {o.get('noticeTitle','')[:90]}")
            if t == "WYNIK":
                naz, nip = wyciagnij_zwyciezce(o)
                print(f"            -> zwyciezca: {naz} (NIP {nip})")

    # Zapis dla STRONY: pelna aktualna lista dopasowanych (nie tylko nowosci)
    zapisz_json(dopasowane)

    zapisz_pamiec(widziane)
    print("Gotowe.")


if __name__ == "__main__":
    main()
