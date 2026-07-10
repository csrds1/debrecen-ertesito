#!/usr/bin/env python3
"""
Debrecen ingatlan- és árverésfigyelő.

Végigmegy egy sor ingatlanos és árverési oldalon, megnézi van-e ÚJ hirdetés
Debrecenre (telek, csarnok/ipari ingatlan), és ha igen, egy összefoglaló
emailt küld róla.

Az már látott hirdetések azonosítóit a state.json fájlban tárolja, hogy
soha ne küldjön kétszer értesítést ugyanarról a hirdetésről.
"""

import json
import os
import re
import smtplib
import ssl
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).parent / "state.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "hu-HU,hu;q=0.9",
}
TIMEOUT = 20

# ---------------------------------------------------------------------------
# Figyelt oldalak konfigurációja.
#
# Minden bejegyzés egy oldalt/keresést ír le:
#   name       - megjelenő név az emailben
#   url        - a lekérdezendő keresési URL
#   id_regex   - regex, ami a hirdetés-linkekre illeszkedik; az első
#                zárójeles csoport lesz a hirdetés egyedi azonosítója
#   must_contain - (opcionális) a linkhez tartozó szövegnek tartalmaznia
#                  kell ezt a szót (pl. "Debrecen"), ha a keresés maga
#                  nem város-specifikus (megyei/országos keresés)
# ---------------------------------------------------------------------------
SITES = [
    {
        "name": "ingatlan.com - Debrecen, telek",
        "url": "https://ingatlan.com/debrecen/elado+telek",
        "id_regex": r"ingatlan\.com/(\d{6,9})",
    },
    {
        "name": "ingatlan.com - Debrecen, ipari ingatlan/csarnok",
        "url": "https://ingatlan.com/debrecen/elado+ipari",
        "id_regex": r"ingatlan\.com/(\d{6,9})",
    },
    {
        "name": "Jófogás - Debrecen, telek",
        "url": "https://ingatlan.jofogas.hu/hajdu-bihar/debrecen/telek-fold",
        "id_regex": r"jofogas\.hu/[\w/]*?_(\d{6,12})\.htm",
    },
    {
        "name": "Jófogás - Hajdú-Bihar, ipari/üzlethelyiség (Debrecenre szűrve)",
        "url": "https://ingatlan.jofogas.hu/hajdu-bihar/iroda-uzlethelyiseg-ipari-ingatlan",
        "id_regex": r"jofogas\.hu/[\w/]*?_(\d{6,12})\.htm",
        "must_contain": "Debrecen",
    },
    {
        "name": "Ingatlanok.hu - Debrecen, ipari ingatlan",
        "url": "https://ingatlanok.hu/elado/ipari_ingatlan/debrecen",
        "id_regex": r"ingatlanok\.hu/[\w\-/]*?(\d{5,9})",
    },
    {
        "name": "Elektronikus Aukciós Rendszer (állami árverés) - ingatlanok",
        "url": "https://e-arveres.mnv.hu/index-meghirdetesek-ingatlan.html",
        "id_regex": r"(\d{4,9})",
        "must_contain": "Debrecen",
    },
]

# Ide írd az emailt küldő és fogadó címeket (a jelszót SOHA ne írd ide,
# az a GitHub Secrets-ből / környezeti változóból jön).
SENDER_EMAIL = os.environ.get("GMAIL_USER", "")
APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("TO_EMAIL", "")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        print(f"  [HIBA] Nem sikerült letölteni: {url} ({exc})", file=sys.stderr)
        return None


def extract_listings(html: str, site: dict) -> list[dict]:
    """Kigyűjti az adott oldal HTML-jéből az egyedi hirdetéseket."""
    soup = BeautifulSoup(html, "lxml")
    id_regex = re.compile(site["id_regex"])
    must_contain = site.get("must_contain")

    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = id_regex.search(href)
        if not match:
            continue

        listing_id = match.group(1)
        text = a.get_text(" ", strip=True)

        # ha van "must_contain" szűrő (pl. megyei keresésnél a "Debrecen"
        # szó), csak akkor fogadjuk el, ha a link szövegében szerepel
        if must_contain and must_contain.lower() not in text.lower():
            continue

        if not text:
            # próbáljunk címet szerezni a kép alt szövegéből
            img = a.find("img")
            text = img.get("alt", "").strip() if img else ""

        full_url = href if href.startswith("http") else f"https://{href.lstrip('/')}"

        # ha ugyanaz az ID többször előfordul (pl. kép + cím külön linkje),
        # tartsuk meg a hosszabb/informatívabb szöveget
        if listing_id not in found or len(text) > len(found[listing_id]["title"]):
            found[listing_id] = {
                "id": listing_id,
                "title": text or "(cím nélküli hirdetés)",
                "url": full_url,
            }

    return list(found.values())


def check_site(site: dict, state: dict) -> list[dict]:
    print(f"Ellenőrzés: {site['name']} ...")
    html = fetch(site["url"])
    if html is None:
        return []

    listings = extract_listings(html, site)
    seen_ids = set(state.get(site["name"], []))
    new_listings = [item for item in listings if item["id"] not in seen_ids]

    all_ids = seen_ids.union(item["id"] for item in listings)
    state[site["name"]] = list(all_ids)

    print(f"  -> {len(listings)} hirdetés találva, ebből {len(new_listings)} új")
    return new_listings


def build_email_body(results: dict) -> str:
    lines = ["Új hirdetéseket találtam a következő keresésekben:\n"]
    for site_name, items in results.items():
        if not items:
            continue
        lines.append(f"\n== {site_name} ==")
        for item in items:
            lines.append(f"- {item['title']}\n  {item['url']}")
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    if not (SENDER_EMAIL and APP_PASSWORD and RECIPIENT_EMAIL):
        print(
            "[HIBA] Hiányzó email beállítás (GMAIL_USER / GMAIL_APP_PASSWORD / "
            "TO_EMAIL környezeti változó). Az emailt nem tudom elküldeni.",
            file=sys.stderr,
        )
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(SENDER_EMAIL, APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, [RECIPIENT_EMAIL], msg.as_string())

    print("Email elküldve.")


def main() -> None:
    state = load_state()
    results = {}
    total_new = 0

    for site in SITES:
        new_items = check_site(site, state)
        results[site["name"]] = new_items
        total_new += len(new_items)
        time.sleep(2)  # ne terheljük túl az oldalakat

    save_state(state)

    if total_new == 0:
        print("Nincs új hirdetés ezúttal.")
        return

    subject = f"🏠 {total_new} új debreceni hirdetés/árverés"
    body = build_email_body(results)
    print(body)
    send_email(subject, body)


if __name__ == "__main__":
    main()
