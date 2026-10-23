#!/usr/bin/env python3
"""Sledovanie prenájmov bytov v Devínskej Novej Vsi.

Raz denne: stiahne výpisy z Nehnuteľnosti.sk, aktualizuje CSV v priečinku data/
a vygeneruje stránku docs/index.html. Spustenie: python tracker.py
"""
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://www.nehnutelnosti.sk"
AREA = "bratislava-devinska-nova-ves/prenajom"
KINDS = ["byty", "garsonky", "1-izbove-byty", "2-izbove-byty",
         "3-izbove-byty", "4-izbove-byty", "5-izbove-byty"]
MAX_PAGES = 5
GONE_AFTER_DAYS = 7      # inzerát zmizol aspoň tak dlho = byt považujeme za obsadený
REMATCH_DAYS = 14        # nový inzerát môže byť obnovením bytu, ktorý zmizol najviac pred toľkými dňami
MATCH_VANISHED = 6       # minimálne skóre zhody s bytom, ktorého inzerát zmizol
MATCH_LIVE = 13          # byt, ktorý je v ponuke aj dnes: spojí sa len pri zhode čísla domu (inak ide o iný byt)
MATCH_SURE = 8           # od tohto skóre je spojenie "isté"
DATA = Path("data")
SITE = Path("docs")
DEBUG = Path("debug")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124 Safari/537.36",
           "Accept-Language": "sk-SK,sk;q=0.9"}
FLAT_FIELDS = ["flat_id", "ids", "fp", "street", "layout", "area", "first_seen",
               "last_seen", "price_first", "price_last", "status", "initial",
               "url", "title", "sig", "merges", "energy", "energy_amt", "parking", "snap", "alts", "cond"]
DAILY_FIELDS = ["date", "active", "new", "gone"]
PRICE_FIELDS = ["date", "flat_id", "price"]
EVENT_FIELDS = ["date", "type", "flat_id", "note"]
MAX_CHECKS = 15          # najviac tolko starých inzerátov sa pri jednom behu ide overiť
ID_RE = re.compile(r"(?:/detail/([A-Za-z0-9]+)|^(?:https?://[^/]+)?/(\d{6,10})(?:[/?#]|$))")  # /detail/ID alebo /1234567/slug


def lid_of(href):
    m = ID_RE.search(href or "")
    return (m.group(1) or m.group(2)) if m else None


DIAG = {"pages": [], "skipped": [], "check": []}      # diagnostika posledného behu (zobrazí sa na stránke)
LOC_RE = re.compile(r"Dev[ií]nska\s+Nov[aá]\s+Ves", re.I)   # adresa inzerátu musí byť v DNV
OKRES_RE = re.compile(r"okres\s+[A-ZÁ-Ž][\w-]*(?:\s+[IVX]+\b)?")
COMM_RE = re.compile(r"komerčn|multifunk|\bpriestor(?:y|u|om|ov)?\b|kancelár|obchodn\w+\s+priestor|nebytov|skladov|sklad\b|reštaurác|prevádzk|garážov\w+\s+státie\s+na\s+prenájom|ordinác|showroom|výrobn|hala\b", re.I)
SKIPPED = [0]                                                  # počet inzerátov z inej lokality
STOP = {"bratislava", "devínska", "devinska", "nová", "nova", "okres", "prenájom",
        "prenajom", "mesiac", "izbový", "izbovy", "izbov", "email", "telefón"}


# ---------------------------------------------------------------- stiahnutie
def next_url(html, page):
    """Nájde v stránke skutočný odkaz na ďalšiu stranu; inak vráti None."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if re.search(r"[?&](?:p%5Bpage%5D|p\[page\]|page)=" + str(page + 1) + r"(?:&|$)", h):
            return BASE + h if h.startswith("/") else h
    return None


def fetch(kind, page, url=None):
    fixture = os.environ.get("TRACKER_FIXTURE_DIR")
    if fixture:
        p = Path(fixture) / (f"{kind}.html" if page == 1 else f"{kind}_{page}.html")
        return p.read_text(encoding="utf-8") if p.exists() else None
    if not url:
        url = f"{BASE}/vysledky/{kind}/{AREA}"
        if page > 1:
            url += f"?p[page]={page}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    time.sleep(2)
    return r.text


# ------------------------------------------------------------------ parsovanie
def make_sig(text):
    """Krátky odtlačok textu: prvých ~40 rôznych slov (na porovnanie popisov)."""
    seen, out = set(), []
    for w in re.findall(r"[^\W\d_]{4,}", text.lower()):
        if w in STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= 40:
            break
    return " ".join(out)


def parse_energy(text):
    """Vráti (režim, suma): 'incl' = energie v cene, 'sep' = zvlášť s uvedenou sumou,
    'sep?' = zvlášť bez sumy, '' = nejasné."""
    t = text.lower()
    amt = None
    m = (re.search(r"(?:energi\w*|zálohy?|služb\w*|poplatk\w*)\s*[:\-]?\s*(?:cca\s*|vo výške\s*)?(\d{2,3})\s*(?:€|eur)", t) or
         re.search(r"(\d{2,3})[,.]?-?\s*(?:€|eur\w*)\s*(?:/\s*mes\w*\.?\s*)?(?:za\s+|na\s+|ako\s+)?"
                   r"(?:zálohy?\s+(?:na\s+|za\s+)?)?(?:energi|poplat|služb)", t))
    if m and 20 <= int(m.group(1)) <= 600:
        amt = int(m.group(1))
    incl = re.search(r"(?:vrátane|vrát\.|včítane)\s+(?:všetkých\s+)?(?:energi|poplatk|služ)|\bs\s+energi|"
                     r"energi\w*\s+(?:sú\s+)?(?:už\s+)?(?:zahrnut|v\s+cene)|"
                     r"v\s+cene\s+(?:sú\s+)?(?:všetky\s+)?(?:energi|poplat)|all\s*inclusive", t)
    apart = re.search(r"bez\s+energi|energi\w*\s+(?:zvlášť|navyše|hradí|platí|nie sú)|\+\s*energi", t)
    if amt and not incl:
        return "sep", amt
    if incl and not amt:
        return "incl", 0
    if amt and incl:
        return "", 0
    if apart:
        return "sep?", 0
    return "", 0


def parse_parking(text):
    """'yes' = vlastné/garážové stát[ií]e (v cene), 'extra' = za príplatok,
    'street' = len parkovanie pred domom/neurčené, '' = nič."""
    t = text.lower()
    if re.search(r"bez\s+(?:vlastn\w+\s+)?(?:parkov|garáž|parking|stát[ií]a)", t):
        return ""
    if re.search(r"nepatr\w*[^.]{0,50}(?:parkov|garáž|stát)|(?:parkov|garáž|stát)[^.]{0,40}nepatr|nie\s+je\s+(?:súčasť|k\s+dispozícii)[^.]{0,40}parkov|žiadne\s+(?:vlastné\s+)?parkov", t):
        return "street" if "verejn" in t else ""
    ded = re.search(r"parkovac\w+\s+(?:miest|stát[ií]|stan)|garáž|garaz|stát[ií]|stání|parking|vlastn\w+\s+parkov", t)
    if not (ded or re.search(r"parkov", t)):
        return ""
    pk = r"(?:parkovac\w*|parkovan\w*|garáž\w*|stát[ií]\w*|parking\w*)"
    if re.search(r"parkov\w*\s+v\s+cene|v\s+cene\s+(?:je\s+|sú\s+)?(?:\w+\s+){0,3}(?:parkov|stát[ií])|stát[ií]\w*\s+v\s+cene|parkovan\w*\s+(?:je\s+)?zahrnut", t):
        return "yes"
    if re.search(pk + r"[^.;]{0,30}?(?:príplatok|poplat|zvlášť|navyše)|" +
                 pk + r"(?:\s+(?:miest\w*|stát[ií]\w*|stan\w*))?[\s,:\-(]*(?:za\s+)?\+?\s*\d{2,3}\s*(?:€|eur)|" +
                 r"(?:\+|plus)\s*\d{2,3}\s*(?:€|eur\w*)(?:\s*/?\s*mes\w*)?\s*(?:za\s+)?" + pk, t):
        return "extra"
    return "yes" if ded else "street"


def parse_condition(text):
    """Stav bytu: new = novostavba, full = kompletná rekonštrukcia, partial = čiastočná,
    reko = rekonštrukcia (rozsah neuvedený), orig = pôvodný stav, '' = neuvedené."""
    t = text.lower()
    if re.search(r"čiastočn\w*\s+(?:zrekonštr|rekonštr)", t):
        return "partial"
    if re.search(r"(?:kompletn|úpln|celkov|totáln)\w*\s+(?:zrekonštr|rekonštr)", t):
        return "full"
    if "novostavb" in t:
        return "new"
    if re.search(r"rekonštruk|zrekonštruovan", t):
        return "reko"
    if re.search(r"pôvodn\w*\s+stav|v\s+pôvodnom", t):
        return "orig"
    return ""


def parse_text(lid, text):
    price = None
    m = re.search(r"(\d{1,3}(?:[\s ]\d{3})+|\d{2,5})\s*€\s*/\s*mes", text)
    if m:
        price = int(re.sub(r"\D", "", m.group(1)))
        if not 50 <= price <= 20000:
            price = None
    area = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m(?:²|2)", text)
    if m:
        area = float(m.group(1).replace(",", "."))
        if not 10 <= area <= 400:
            area = None
    layout, best = None, 10 ** 9
    for pat in (r"(garsón\w*)", r"(\d(?:[.,]5)?)\s*-?\s*izb", r"\b(\d)\s*i\s+byt"):
        m = re.search(pat, text, re.I)
        if m and m.start() < best:
            best = m.start()
            g = m.group(1)
            layout = "1" if g.lower().startswith("gars") else g.replace(",", ".")
    street = ""
    U, L = "A-ZÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ", "a-záäčďéíĺľňóôŕšťúýž"
    m = re.search(rf"((?:[{U}][{L}.\-]+\s+){{0,2}}[{U}][{L}.\-]+(?:\s+\d+[A-Za-z0-9/]*)?)"
                  r"(?:\s*\([^)]*\))?\s*,\s*(?:Bratislava\s*-\s*)?Devínska Nová Ves", text)
    if m:
        street = m.group(1).strip()
    head = text[:120].lower()
    status = "REZERVOVANÉ" if "rezervovan" in head else ("PRENAJATÉ" if "prenajat" in head else "")
    en, ea = parse_energy(text)
    return {"id": lid, "price": price, "area": area, "layout": layout,
            "street": street, "status": status, "sig": make_sig(text),
            "energy": en, "energy_amt": ea, "parking": parse_parking(text), "snap": text[:300],
            "cond": parse_condition(text)}


def score(it):
    return sum(it[k] not in (None, "") for k in ("price", "area", "layout", "street"))


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    found, urls, titles = {}, {}, {}
    for a in soup.find_all("a", href=ID_RE):
        lid = lid_of(a["href"])
        href = a["href"].split("?")[0].split("#")[0]
        urls.setdefault(lid, BASE + href if href.startswith("/") else href)
        label = " ".join(a.get_text(" ", strip=True).split())
        if len(label) > len(titles.get(lid, "")):
            titles[lid] = label[:100]
        node = a
        # kartu = najväčší predok, ktorý obsahuje odkazy len na jeden inzerát
        while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
            ids = {lid_of(x["href"])
                   for x in node.parent.find_all("a", href=ID_RE)}
            if len(ids) > 1:
                break
            node = node.parent
        text = " ".join(node.get_text(" ", strip=True).split())
        ok = [m for m in OKRES_RE.finditer(text)]
        if ok:       # prvá lokalita v karte musí byť DNV; všetko za ňou (cudzie reklamy) sa odreže
            if not LOC_RE.search(text[max(0, ok[0].start() - 70):ok[0].end()]):
                text = ""
            else:
                text = text[:ok[0].end() + 40]
        if not LOC_RE.search(text):      # odporúčané/sponzorované inzeráty z iných častí mesta
            SKIPPED[0] += 1
            if len(DIAG["skipped"]) < 40:
                DIAG["skipped"].append([lid, urls.get(lid, ""), text[:220]])
            continue
        if COMM_RE.search(titles.get(lid, "") + " " + text[:200]):   # komerčné priestory nie sú byty
            SKIPPED[0] += 1
            if len(DIAG["skipped"]) < 40:
                DIAG["skipped"].append([lid, urls.get(lid, ""), "(komerčný) " + text[:200]])
            continue
        item = parse_text(lid, text)
        if lid not in found or score(item) > score(found[lid]):
            found[lid] = item
    for lid, it in found.items():
        it["url"], it["title"] = urls.get(lid, ""), titles.get(lid, "")
    return list(found.values())


def scrape():
    items, raw = {}, []
    for kind in KINDS:
        seen_before = set()
        url = None
        total = {}
        for page in range(1, MAX_PAGES + 1):
            html = fetch(kind, page, url)
            if not html:
                break
            raw.append(html)
            got = parse(html)
            new_ids = {i["id"] for i in got} - seen_before
            nxt = next_url(html, page)
            if page == 1:
                m = re.search(r"(\d[\d\s\u00a0]{0,5}?)\s*nehnuteľnos", BeautifulSoup(html, "html.parser").get_text(" "))
                total[kind] = int(re.sub(r"\D", "", m.group(1))) if m else None
            DIAG["pages"].append([kind, page, len(got), len(new_ids), "odkaz" if url else ("hádaný" if page > 1 else "")])
            url = nxt
            seen_before |= new_ids
            for it in got:
                if it["id"] not in items or score(it) > score(items[it["id"]]):
                    items[it["id"]] = it
            if not new_ids or (len(got) < 15 and not nxt):
                break
        DIAG["check"].append([kind, total.get(kind), len(seen_before)])
        if total.get(kind) is not None and total[kind] != len(seen_before):
            print(f"POZOR: {kind}: stránka uvádza {total[kind]} nehnuteľností, ale našla som {len(seen_before)}")
    if not items:
        DEBUG.mkdir(exist_ok=True)
        if raw:
            (DEBUG / "last.html").write_text(raw[0], encoding="utf-8")
    return list(items.values())


# ---------------------------------------------------------------------- dáta
def read_csv(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows):
    DATA.mkdir(exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fingerprint(it):
    # presný odtlačok, keď inzerát uvádza číslo domu: dispozícia | m² | ulica číslo
    m = re.search(r"([^\W\d_]+)\s+(\d+[A-Za-z/]*)\s*$", it["street"])
    if it["layout"] and it["area"] and m:
        return f"{it['layout']}|{round(it['area'])}|{m.group(1).lower()} {m.group(2).lower()}"
    return ""


def street_key(street):
    tokens = re.findall(r"[^\W\d_]{3,}", street.lower())
    return tokens[-1] if tokens else ""


def jaccard(a, b):
    a, b = set(a.split()), set(b.split())
    if len(a) < 5 or len(b) < 5:
        return None
    return len(a & b) / len(a | b)


def match_score(it, f, fp):
    """Skóre, že inzerát `it` je ten istý byt ako `f` (None = určite nie)."""
    if not it["layout"] or not f["layout"] or it["layout"] != f["layout"]:
        return None
    s = 0
    a1, a2 = it["area"], num(f["area"])
    if a1 and a2:
        d = abs(a1 - a2)
        if d > 3:
            return None
        s += 2 if d <= 1 else 1
    k1, k2 = street_key(it["street"]), street_key(f["street"])
    if k1 and k2:
        if k1 != k2:
            return None
        s += 2
    if fp and fp == f["fp"]:
        s += 4
    p1, p2 = it["price"], num(f["price_last"])
    if p1 and p2:
        r = abs(p1 - p2) / p2
        s += 1 if r <= 0.15 else (-1 if r > 0.3 else 0)
    j = jaccard(it.get("sig", ""), f.get("sig", ""))
    if j is not None:
        s += 4 if j >= 0.6 else (2 if j >= 0.4 else (-2 if j < 0.15 else 0))
    return s


def find_match(it, fp, flats, today, present, claimed):
    t = today.isoformat()
    best_s, best_f = 0, None
    for f in flats:
        live = f["last_seen"] == t or f["flat_id"] in present
        if live:
            need = MATCH_LIVE
        else:
            if f["flat_id"] in claimed or (today - date.fromisoformat(f["last_seen"])).days > REMATCH_DAYS:
                continue
            need = MATCH_VANISHED
        s = match_score(it, f, fp)
        if s is not None and s >= need and (best_f is None or s > best_s):
            best_s, best_f = s, f
    return best_f, best_s


DETAIL_MAX = 60           # najviac toľko detailov sa stiahne pri jednom behu
DETAIL_TTL = 21           # po koľkých dňoch sa detail znova prečíta


def script_text(html):
    """Text z JSON skriptov stránky (__NEXT_DATA__, ld+json) – záloha, keď je údaj len tam."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for sc in soup.find_all("script"):
        if sc.get("id") == "__NEXT_DATA__" or "json" in (sc.get("type") or ""):
            out.append((sc.string or sc.get_text() or "")[:20000])
    return " ".join(out).replace('\\"', '"')


def detail_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    t = " ".join(soup.get_text(" ", strip=True).split())
    t = t[:7000]       # hlavička s cenou a energiami je pred popisom
    # hlavička „530 €/mes. s energiami“ býva v rozpore s popisom, preto sa ignoruje
    return re.sub(r"\d[\d\s]*€\s*/\s*mes\.?\s*s\s+energi\w*", " ", t)


def detail_cond(t):
    """Štítok „Novostavba“ v parametroch inzerátu má prednosť pred zmienkami v texte."""
    if re.search(r"(?<![\w-])Novostavba(?![\w])", t[:3000]):
        return "new"
    return parse_condition(t[:4000])


def enrich(items, today):
    """Z detailu inzerátu doplní energie, parkovanie a stav (v zozname karty ich nie je)."""
    path = DATA / "details.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        cache = {}
    fixture = os.environ.get("TRACKER_FIXTURE_DIR")
    n = ok = 0
    for it in items:
        c = cache.get(it["id"])
        if c and c.get("v") == 5 and (today - date.fromisoformat(c["d"])).days < DETAIL_TTL:
            pass
        elif n < DETAIL_MAX and it.get("url"):
            n += 1
            html = None
            try:
                if fixture:
                    f = Path(fixture) / f"detail_{it['id']}.html"
                    html = f.read_text(encoding="utf-8") if f.exists() else None
                else:
                    r = requests.get(it["url"], headers=HEADERS, timeout=30)
                    time.sleep(2)
                    html = r.text if r.status_code == 200 else None
            except requests.RequestException:
                html = None
            if html:
                t = detail_text(html)
                en, ea = parse_energy(t)
                if not en:       # záloha: údaj môže byť len v JSON skripte stránky
                    en, ea = parse_energy(script_text(html))
                if len(DIAG.setdefault("samples", [])) < 6:
                    DIAG["samples"].append([it["url"], len(t), en, ea, t[:260]])
                c = {"v": 5, "d": today.isoformat(), "energy": en, "energy_amt": ea,
                     "parking": parse_parking(t), "cond": detail_cond(t)}
                cache[it["id"]] = c
        if c:
            ok += 1
            it["energy"], it["energy_amt"], it["parking"] = c["energy"], c["energy_amt"], c["parking"]
            if c["cond"]:
                it["cond"] = c["cond"]
    DATA.mkdir(exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    DIAG["detail"] = [n, ok, len(items)]
    print(f"detaily: stiahnutých {n}, doplnených {ok} z {len(items)}")


def check_listing(url):
    """Pozrie starý inzerát a vráti krátky popis stavu."""
    lid = lid_of(url)
    if not lid:
        return "inzerát nemá odkaz"
    fixture = os.environ.get("TRACKER_FIXTURE_DIR")
    if fixture:
        p = Path(fixture) / f"detail_{lid}.html"
        if not p.exists():
            return "stránka inzerátu už neexistuje (404)"
        html = p.read_text(encoding="utf-8")
    else:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            time.sleep(2)
        except requests.RequestException:
            return "nepodarilo sa overiť"
        if r.status_code in (404, 410):
            return "stránka inzerátu už neexistuje (404)"
        if r.status_code != 200:
            return f"nepodarilo sa overiť (HTTP {r.status_code})"
        html = r.text
    t = " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split()).lower()
    if "už nie je aktuálny" in t or "uz nie je aktualny" in t:
        return "inzerát je označený ako prenajatý" if "prenajat" in t[:1500] else "inzerát už nie je aktuálny"
    return "inzerát je stále dostupný"


def finish_events(flats, events, today):
    """Overí zmiznuté inzeráty, uloží udalosti do data/events.csv a vráti všetky."""
    t = today.isoformat()
    by = {f["flat_id"]: f for f in flats}
    n = 0
    for e in events:
        if e["type"] in ("gone", "rented") and n < MAX_CHECKS:
            fl = by.get(e["flat_id"])
            if fl and fl.get("url"):
                n += 1
                try:
                    res = check_listing(fl["url"])
                except Exception:
                    res = "nepodarilo sa overiť"
                e["note"] = "overenie: " + res
                if res.endswith("stále dostupný"):
                    e["note"] += " (pozor: možno ho výpis len vynechal)"
    allv = [r for r in read_csv(DATA / "events.csv") if r["date"] != t] + events
    write_csv(DATA / "events.csv", EVENT_FIELDS, allv)
    return allv


EXCLUDE_IDS = {"JuOvYGdou7w"}      # natvrdo vyradené inzeráty (ďalšie ID patria do data/exclude.txt)


def load_exclude():
    ex = set(EXCLUDE_IDS)
    p = DATA / "exclude.txt"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                ex.add(lid_of(line) or line)
    return ex


def purge(ex):
    """Vymaže z uložených dát všetko k vyradeným inzerátom a komerčným priestorom."""
    flats = read_csv(DATA / "listings.csv")
    marker = DATA / "purged_commercial.txt"
    first = not marker.exists()      # kontrola komerčných priestorov podľa textu prebehne len raz
    if first and flats:
        marker.write_text("hotovo\n", encoding="utf-8")
    if not flats:
        return
    bad = set()
    for f in flats:
        ids = set(f["ids"].split("|")) | {f["flat_id"]}
        if ids & ex or (first and COMM_RE.search((f.get("title") or "") + " " + (f.get("snap") or "")[:200])):
            bad.add(f["flat_id"])
    if not bad:
        return
    write_csv(DATA / "listings.csv", FLAT_FIELDS, [f for f in flats if f["flat_id"] not in bad])
    for name, fields in (("prices.csv", PRICE_FIELDS), ("events.csv", EVENT_FIELDS)):
        rows = read_csv(DATA / name)
        write_csv(DATA / name, fields, [r for r in rows if r["flat_id"] not in bad])
    dp = DATA / "details.json"
    if dp.exists():
        try:
            c = json.loads(dp.read_text(encoding="utf-8"))
            for f in flats:
                if f["flat_id"] in bad:
                    for i in f["ids"].split("|"):
                        c.pop(i, None)
            dp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        except ValueError:
            pass
    print(f"vyčistené dáta: odstránených {len(bad)} bytov (komerčné/vyradené)")


def load_no_merge():
    """ID inzerátov z data/no_merge.txt sa nikdy nespoja s iným bytom (jedno ID na riadok)."""
    p = DATA / "no_merge.txt"
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.add(line)
    return out


def split_merged(flats, nm):
    """Rozdelí späť inzeráty, ktoré používateľ označil ako samostatný byt."""
    for f in list(flats):
        ids = f["ids"].split("|")
        if not any(i in nm for i in ids[1:]):
            continue
        alts = json.loads(f.get("alts") or "[]")
        keep = [ids[0]]
        for i in ids[1:]:
            if i not in nm:
                keep.append(i)
                continue
            a = next((x for x in alts if x["id"] == i), {})
            new = dict(f)
            new.update({"flat_id": i, "ids": i, "fp": "", "merges": "", "alts": "",
                        "url": a.get("url") or f["url"],
                        "price_last": a.get("price") or f["price_last"],
                        "price_first": a.get("price") or f["price_first"],
                        "area": a.get("area") or f["area"],
                        "first_seen": a.get("date") or f["first_seen"]})
            flats.append(new)
        f["ids"] = "|".join(keep)
        f["merges"] = "|".join(m for m in f["merges"].split("|") if m and m.split(":")[0] not in nm)
        f["alts"] = json.dumps([x for x in alts if x["id"] not in nm], ensure_ascii=False) if alts else ""


def update(items, today):
    t = today.isoformat()
    flats = read_csv(DATA / "listings.csv")
    for f in flats:
        for k in FLAT_FIELDS:
            f.setdefault(k, "")
    nm = load_no_merge()
    split_merged(flats, nm)
    daily = [r for r in read_csv(DATA / "daily.csv") if r["date"] != t]
    prices = [r for r in read_csv(DATA / "prices.csv") if r["date"] != t]
    first_run = not flats
    by_id = {}
    for f in flats:
        for i in f["ids"].split("|"):
            by_id[i] = f
    prev = max((r["date"] for r in daily), default=None)
    present = {by_id[i["id"]]["flat_id"] for i in items if i["id"] in by_id}
    claimed = set()
    events, merged_today = [], set()

    def ev(typ, fl, note=""):
        events.append({"date": t, "type": typ, "flat_id": fl["flat_id"], "note": note})

    today_flats = {}   # flat_id -> (flat row, cena, inzerát) s najnižšou cenou
    created = 0
    for it in items:
        fl = by_id.get(it["id"])
        fp = fingerprint(it)
        if fl is None and it["id"] not in nm:
            cand, sc = find_match(it, fp, flats, today, present, claimed)
            if cand is not None:
                fl = cand
                if fl["last_seen"] != t and fl["flat_id"] not in present:
                    claimed.add(fl["flat_id"])
                fl["ids"] += "|" + it["id"]
                fl["merges"] = (fl["merges"] + "|" if fl["merges"] else "") + f"{it['id']}:{sc}"
                alts = json.loads(fl["alts"] or "[]")
                alts.append({"id": it["id"], "sc": sc, "url": it.get("url", ""), "price": it["price"] or "",
                             "area": it["area"] or "", "date": t, "snap": it.get("snap", "")[:160]})
                fl["alts"] = json.dumps(alts, ensure_ascii=False)
                merged_today.add(fl["flat_id"])
                ev("relist", fl, f"nový inzerát {it['id']}, zhoda {sc}")
                by_id[it["id"]] = fl
        if fl is None:
            fl = {"flat_id": it["id"], "ids": it["id"], "fp": fp, "street": it["street"],
                  "layout": it["layout"] or "", "area": it["area"] or "",
                  "first_seen": t, "last_seen": t,
                  "price_first": it["price"] or "", "price_last": it["price"] or "",
                  "status": it["status"], "initial": "1" if first_run else "0",
                  "url": it.get("url", ""), "title": it.get("title", ""),
                  "sig": it.get("sig", ""), "merges": "", "alts": "", "cond": it.get("cond", ""),
                  "energy": it.get("energy", ""), "energy_amt": it.get("energy_amt") or "",
                  "parking": it.get("parking", "")}
            flats.append(fl)
            by_id[it["id"]] = fl
            created += 1
            if not first_run:
                ev("new", fl)
            if it["price"]:
                prices.append({"date": t, "flat_id": fl["flat_id"], "price": it["price"]})
        cur = today_flats.get(fl["flat_id"])
        price = it["price"]
        if cur is None or (price and (cur[1] is None or price < cur[1])):
            today_flats[fl["flat_id"]] = (fl, price, it)

    for fl, price, it in today_flats.values():
        if fl["last_seen"] != t or fl["first_seen"] != t:
            if price and str(price) != str(fl["price_last"]):
                prices.append({"date": t, "flat_id": fl["flat_id"], "price": price})
                if fl["price_last"]:
                    ev("price", fl, f"{int(float(fl['price_last']))} → {price} €")
        if prev and fl["last_seen"] < prev and fl["flat_id"] not in merged_today and fl["first_seen"] != t:
            ev("back", fl, f"chýbal od {fl['last_seen']}")
        fl["last_seen"] = t
        if price:
            fl["price_last"] = price
            if not fl["price_first"]:
                fl["price_first"] = price
        fl["status"] = it["status"]
        if it.get("url"):
            fl["url"], fl["title"] = it["url"], it.get("title", "")
        if it.get("sig"):
            fl["sig"] = it["sig"]
        fl["energy"], fl["energy_amt"] = it.get("energy", ""), it.get("energy_amt") or ""
        fl["parking"] = it.get("parking", "")
        fl["snap"] = it.get("snap", "")
        if it.get("cond"):
            fl["cond"] = it["cond"]
        for k in ("street", "layout", "area"):
            if not fl[k] and it[k]:
                fl[k] = it[k]
        if not fl["fp"]:
            fl["fp"] = fingerprint(it)

    for f in flats:
        if prev and f["last_seen"] == prev and f["last_seen"] != t:
            ev("gone", f)
    rented_done = {r["flat_id"] for r in read_csv(DATA / "events.csv") if r["type"] == "rented" and r["date"] != t}
    for f in flats:
        if (f["last_seen"] != t and f["flat_id"] not in rented_done and
                (today - date.fromisoformat(f["last_seen"])).days >= GONE_AFTER_DAYS):
            ev("rented", f)
    gone = sum(1 for f in flats if prev and f["last_seen"] == prev and f["last_seen"] != t)
    daily.append({"date": t, "active": len(today_flats), "new": 0 if first_run else created, "gone": gone})
    daily.sort(key=lambda r: r["date"])
    write_csv(DATA / "listings.csv", FLAT_FIELDS, flats)
    write_csv(DATA / "daily.csv", DAILY_FIELDS, daily)
    write_csv(DATA / "prices.csv", PRICE_FIELDS, prices)
    return flats, daily, events


# ------------------------------------------------------------------- stránka
def build_data(flats, daily, today, events):
    scores = [int(m.split(":")[1]) for f in flats for m in f["merges"].split("|") if m]
    pos = {f["flat_id"]: i for i, f in enumerate(flats)}
    return {
        "updated": today.isoformat(),
        "start": daily[0]["date"] if daily else today.isoformat(),
        "gone_after": GONE_AFTER_DAYS,
        "diag": DIAG,
        "merges": {"n": len(scores), "weak": sum(1 for s in scores if s < MATCH_SURE)},
        # [prvýkrát, naposledy, dispozícia, m², nájom, počiatočný(1/0), odkaz, ulica, energie, suma energií, parkovanie, text z posledného videnia, stav]
        "flats": [[f["first_seen"], f["last_seen"], f["layout"], num(f["area"]), num(f["price_last"]),
                   int(f["initial"] or 0), f["url"], f["street"], f["energy"], num(f["energy_amt"]) or 0, f["parking"], f["snap"], f.get("cond", "")]
                  for f in flats],
        # [dátum, typ, index bytu, poznámka] od najnovších
        "merged": [[pos[f["flat_id"]], json.loads(f["alts"])] for f in flats if f["alts"]],
        "events": [[e["date"], e["type"], pos[e["flat_id"]], e["note"]]
                   for e in sorted(events, key=lambda e: e["date"], reverse=True)[:400] if e["flat_id"] in pos],
    }


PAGE = r"""<!doctype html>
<html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DNV prenájmy</title>
<style>
:root{color-scheme:light;--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--line:rgba(11,11,11,.12);--s1:#2a78d6;--s2:#eb6834}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:rgba(240,239,236,.14);--s1:#3987e5;--s2:#d95926}}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--ink2);font-size:14px;margin:0 0 14px}
.bar{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-bottom:10px;font-size:14px}
select,input[type=date]{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
a{color:var(--s1)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:12px 0 20px}
.tile{border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:26px;font-weight:600}.tile span{color:var(--ink2);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:16px}
@media(max-width:480px){.grid{grid-template-columns:1fr}}
.card{border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{font-size:15px;margin:0 0 2px}.card p{margin:0 0 10px;color:var(--ink2);font-size:13px}
.box{position:relative;height:240px}.empty{color:var(--ink2);font-size:14px;padding:70px 0;text-align:center}
.scroll{overflow-x:auto}details{margin-top:20px}summary{cursor:pointer;color:var(--ink2)}
table{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}
td,th{border-bottom:1px solid var(--line);padding:4px 8px;text-align:right;white-space:nowrap}
th:first-child,td:first-child,td.l,th.l{text-align:left}
.note{color:var(--ink2);font-size:13px;margin-top:18px}
.gauge{margin-bottom:20px}.gauge h2{font-size:20px;margin:0 0 2px}.gauge p{margin:2px 0;font-size:14px;color:var(--ink2)}
.scale{position:relative;display:flex;height:10px;border-radius:5px;margin:14px 0 4px}
.scale i{display:block;height:100%}.scale i:first-child{border-radius:5px 0 0 5px}.scale i:nth-child(3){border-radius:0 5px 5px 0}
.s1{background:color-mix(in srgb,var(--s1) 22%,transparent)}.s2{background:color-mix(in srgb,var(--s1) 50%,transparent)}.s3{background:color-mix(in srgb,var(--s2) 55%,transparent)}
.scale b{position:absolute;top:-4px;width:4px;height:18px;margin-left:-2px;border-radius:2px;background:var(--ink)}
.scl{display:flex;font-size:12px;color:var(--ink2)}.scl span:nth-child(2){text-align:center}.scl span:nth-child(3){text-align:right}
button.tile{font:inherit;color:inherit;background:none;text-align:left;cursor:pointer}
button.tile:hover{border-color:var(--s1)}summary{cursor:pointer}details.card summary b{font-size:16px}
#evt td:last-child{white-space:normal;min-width:220px}
</style></head><body><main>
<h1>Prenájmy bytov v Devínskej Novej Vsi</h1>
<p class="sub" id="upd"></p>
<div class="bar">
<label>Obdobie <select id="per"><option value="7">7 dní</option><option value="30">30 dní</option><option value="90">90 dní</option><option value="all">Od začiatku zberu</option><option value="custom">Vlastné…</option></select></label>
<span id="cust" class="bar" style="margin:0" hidden><label>od <input type="date" id="d1"></label><label>do <input type="date" id="d2"></label></span>
<label>Dispozícia <select id="lay"></select></label>
<label>Cena <select id="pm"><option value="total">celkom s energiami</option><option value="rent">len nájom bez energií</option></select></label>
<label>Parkovanie <select id="pkf"><option value="">všetky</option><option value="yes">áno (v cene)</option><option value="extra">za príplatok</option><option value="no">bez / neuvedené</option></select></label>
<label>Stav <select id="cdf"><option value="">všetky</option><option value="new">novostavba</option><option value="full">kompletná rekonštrukcia</option><option value="partial">čiastočná rekonštrukcia</option><option value="reko">po rekonštrukcii (rozsah neuvedený)</option><option value="orig">pôvodný stav</option><option value="none">neuvedený</option></select></label>
</div>
<p class="sub" id="cap"></p>
<p class="sub" id="pkcmp"></p>
<div class="tiles" id="tiles"></div>
<div class="card gauge" id="gauge"></div>
<div class="grid">
<div class="card"><h2>Aktívne inzeráty</h2><p>Počet bytov v ponuke v daný deň</p><div class="box" id="b1"><canvas id="c1"></canvas></div></div>
<div class="card"><h2>Pribúdajú alebo ubúdajú byty?</h2><p id="c2txt"></p><div class="box" id="b2"><canvas id="c2"></canvas></div></div>
<div class="card"><h2>Medián dní do obsadenia</h2><p>Podľa dispozície; len byty, ktoré sme videli pribudnúť aj zmiznúť</p><div class="box" id="b3"><canvas id="c3"></canvas></div></div>
<div class="card"><h2>Medián nájmu za m²</h2><p>€/m² bytov, ktoré boli v ponuke v danom období (podľa zvolenej ceny hore)</p><div class="box" id="b4"><canvas id="c4"></canvas></div></div>
</div>
<details class="card" id="listbox" style="margin-top:16px"><summary><b>Zoznam inzerátov</b> <span class="sub" style="display:inline;margin:0">(rozbal kliknutím, alebo klikni na číslo hore)</span></summary>
<p class="sub" style="margin-top:8px">Klikni na dispozíciu alebo „otvoriť“, inzerát sa otvorí v novom okne. Keď podržíš myš nad dispozíciou, uvidíš text inzerátu z posledného videnia.</p>
<div class="bar"><label><input type="radio" name="w" value="act" checked> Aktívne</label>
<label><input type="radio" name="w" value="new"> Nové v období</label>
<label><input type="radio" name="w" value="gone"> Zmizli (obsadené) v období</label></div>
<div class="scroll"><table id="lst"><thead><tr><th>Dispozícia</th><th class="l">Ulica</th><th>m²</th><th>Nájom</th><th>Energie</th><th>Spolu</th><th>Parkovanie</th><th>Stav</th><th id="dh">Od</th><th></th></tr></thead><tbody></tbody></table></div></details>
<details class="card" id="evbox" style="margin-top:16px"><summary><b>Udalosti</b> (<span id="evn">0</span>) <span class="sub" style="display:inline;margin:0">nové, zmiznuté, obsadené a zmeny cien, aby si vedela skontrolovať, ktorý byt to bol</span></summary>
<div class="scroll"><table id="evt"><thead><tr><th>Dátum</th><th class="l">Udalosť</th><th class="l">Byt</th><th class="l">Overenie / poznámka</th></tr></thead><tbody></tbody></table></div></details>
<details class="card" id="mgbox" style="margin-top:16px"><summary><b>Spojené inzeráty</b> (<span id="mgn">0</span>) <span class="sub" style="display:inline;margin:0">tie, ktoré som zlúčila ako jeden byt, skontroluj</span></summary>
<p class="sub" style="margin-top:8px">Sem patria inzeráty, ktoré vyzerali ako ten istý byt (rovnaká dispozícia, plocha, ulica, cena a podobný text). Ak ide o dva rôzne byty, pridaj ID druhého inzerátu na nový riadok do súboru <code>data/no_merge.txt</code> v repozitári. Pri ďalšom behu sa byty rozdelia a už sa nespoja.</p>
<div id="mglist"></div></details>
<details class="card" id="dgbox" style="margin-top:16px"><summary><b>Diagnostika posledného behu</b> <span class="sub" style="display:inline;margin:0">koľko inzerátov sa našlo na ktorej strane a čo sa preskočilo</span></summary>
<div id="dglist" class="sub" style="margin-top:8px"></div></details>
<details><summary>Tabuľka po dňoch</summary><div class="scroll">
<table id="tbl"><thead><tr><th>Dátum</th><th>Aktívne</th><th>Nové</th><th>Zmizli</th></tr></thead><tbody></tbody></table></div></details>
<p class="note" id="note"></p>
</main>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"></script>
<script>
const D = __DATA__;
const $ = id => document.getElementById(id);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const esc = x => String(x == null ? '' : x).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const lab = l => l === '' ? '?' : (l === '1' ? 'Garsónka / 1i' : l + 'i');
const addD = (s, n) => { const d = new Date(s + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
const diff = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 864e5);
const monday = s => addD(s, -((new Date(s + 'T00:00:00Z').getUTCDay() + 6) % 7));
const med = a => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y), m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const fmt = n => n == null ? '–' : String(Math.round(n * 10) / 10).replace('.', ',');
const store = {get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
               set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }};
const T = D.updated, CUT = addD(T, -D.gone_after);
const F = D.flats.map(r => ({first: r[0], last: r[1], lay: lab(r[2]), area: r[3], p: r[4], init: r[5], url: r[6], street: r[7], en: r[8], ea: r[9], pk: r[10], snap: r[11], cd: r[12]}));
const total = f => !f.p ? null : (f.en === 'incl' ? f.p : (f.en === 'sep' && f.ea ? f.p + f.ea : null));
const rentOnly = f => (f.p && (f.en === 'sep' || f.en === 'sep?')) ? f.p : null;
const pr = f => $('pm').value === 'rent' ? rentOnly(f) : total(f);
const pkTxt = f => ({yes: 'áno', extra: 'za príplatok', street: 'pred domom / možné'}[f.pk] || '–');
const cdTxt = f => ({new: 'novostavba', full: 'kompletná rekonštr.', partial: 'čiastočná rekonštr.', reko: 'po rekonštr.', orig: 'pôvodný stav'}[f.cd] || '–');
const enTxt = f => f.en === 'incl' ? 'v cene' : (f.en === 'sep' ? '+' + f.ea + ' €' : (f.en === 'sep?' ? 'zvlášť (?)' : '?'));
let charts = [];

const lays = [...new Set(F.map(f => f.lay))].sort((a, b) => a.localeCompare(b, 'sk', {numeric: true}));
$('lay').innerHTML = '<option value="">Všetky</option>' + lays.map(k => '<option>' + esc(k) + '</option>').join('');
$('d1').value = D.start; $('d2').value = T;
[$('d1'), $('d2')].forEach(i => { i.min = D.start; i.max = T; });
const sp = store.get('per'); if (sp && [...$('per').options].some(o => o.value === sp)) $('per').value = sp; else $('per').value = '30';
const spm = store.get('pm'); if (spm === 'rent' || spm === 'total') $('pm').value = spm;
const sl = store.get('lay'); if (sl && lays.includes(sl)) $('lay').value = sl;
$('upd').textContent = 'Aktualizované ' + T;

function range() {
  const v = $('per').value; let from, to = T;
  if (v === 'custom') { from = $('d1').value || D.start; to = $('d2').value || T; if (from > to) [from, to] = [to, from]; }
  else if (v === 'all') from = D.start; else from = addD(T, -(+v - 1));
  if (from < D.start) from = D.start;
  if (to > T) to = T;
  if (to < from) to = from;
  return [from, to];
}

function make(id, boxId, type, labels, sets, minPoints, legend) {
  const box = $(boxId);
  if (labels.length < minPoints) { box.innerHTML = '<div class="empty">Zatiaľ málo dát pre toto obdobie.</div>'; return; }
  if (typeof Chart === 'undefined') { box.innerHTML = '<div class="empty">Grafy sa nenačítali (chýba pripojenie). Čísla a zoznam fungujú.</div>'; return; }
  const ink2 = css('--ink2'), line = css('--line');
  charts.push(new Chart($(id), {
    type: type,
    data: {labels: labels, datasets: sets.map(s => Object.assign({
      borderColor: css(s.c), backgroundColor: css(s.c), borderWidth: 2, tension: 0, spanGaps: true,
      pointRadius: labels.length > 40 ? 0 : 3, pointHoverRadius: 5,
      borderRadius: type === 'bar' ? {topLeft: 4, topRight: 4} : 0,
      borderSkipped: 'bottom', maxBarThickness: 28}, s.o))},
    options: {responsive: true, maintainAspectRatio: false, animation: false,
      interaction: {mode: 'index', intersect: false},
      plugins: {legend: {display: legend, labels: {color: ink2, boxWidth: 12, boxHeight: 12}}},
      scales: {x: {ticks: {color: ink2, maxRotation: 0, autoSkip: true}, grid: {display: false}, border: {color: line}},
               y: {beginAtZero: true, ticks: {color: ink2, precision: id === 'c4' ? undefined : 0}, grid: {color: line}, border: {display: false}}}}
  }));
}

function renderMerged() {
  $('mgn').textContent = D.merged.length;
  (function(){
    const g = D.diag || {pages: [], skipped: []}, esc = t => String(t).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    let h = '<p>Strany: ' + (g.pages.map(p => esc(p[0]) + ' s.' + p[1] + ': ' + p[2] + ' (nových ' + p[3] + (p[4] ? ', ' + p[4] : '') + ')').join(' · ') || 'nič') + '</p>';
    h += '<p>Kontrola počtu (stránka uvádza / našla som): ' + ((g.check || []).map(c => esc(c[0]) + ' ' + (c[1] === null ? '?' : c[1]) + ' / ' + c[2] + (c[1] !== null && c[1] !== c[2] ? ' ⚠' : ' ✓')).join(' · ') || 'nič') + '</p>';
    h += '<p>Detaily (stiahnuté teraz / doplnené / spolu): ' + esc((g.detail || ['?']).join(' / ')) + '</p>';
    h += (g.samples || []).map(x => '<p><a href="' + esc(x[0]) + '" target="_blank" rel="noopener">detail</a> dĺžka ' + x[1] + ', energie ' + esc(x[2]) + ' ' + esc(x[3]) + '<br>' + esc(x[4]) + '</p>').join('');
    h += '<p>Preskočené (iná lokalita): ' + g.skipped.length + '</p>';
    h += g.skipped.map(x => '<p><a href="' + esc(x[1]) + '" target="_blank" rel="noopener">' + esc(x[0]) + '</a> ' + esc(x[2]) + '</p>').join('');
    $('dglist').innerHTML = h;
  })();
  $('mglist').innerHTML = D.merged.length ? D.merged.map(m => {
    const f = F[m[0]], link = u => /^https:\/\//.test(u) ? '<a href="' + esc(u) + '" target="_blank" rel="noopener">' : '';
    const head = esc(f.lay) + ' · ' + esc(f.street || '—') + ' · ' + (f.area == null ? '?' : esc(f.area)) + ' m² · ' + (f.p ? esc(f.p) + ' €' : '?');
    const alts = m[1].map(a => '<li>' + link(a.url) + 'inzerát ' + esc(a.id) + (link(a.url) ? '</a>' : '') + ' · ' +
      (a.area ? esc(a.area) + ' m² · ' : '') + (a.price ? esc(a.price) + ' € · ' : '') + 'spojený ' + esc(a.date) + ' · zhoda ' + esc(a.sc) +
      (a.sc >= 8 ? ' (istá)' : ' (menej istá)') + (a.snap ? '<br><span class="sub">' + esc(a.snap) + '</span>' : '') + '</li>').join('');
    return '<p style="margin:10px 0 2px"><b>' + (link(f.url) ? link(f.url) + head + '</a>' : head) + '</b></p><ul style="margin:0 0 8px">' + alts + '</ul>';
  }).join('') : '<p class="sub">Zatiaľ nebol spojený žiadny inzerát.</p>';
}

function refresh() {
  charts.forEach(c => c.destroy()); charts = [];
  ['b1', 'b2', 'b3', 'b4'].forEach((b, i) => { $(b).innerHTML = '<canvas id="c' + (i + 1) + '"></canvas>'; });
  const [from, to] = range(), L = $('lay').value;
  const PK = $('pkf').value;
  const base = F.filter(f => !L || f.lay === L);
  const CD = $('cdf').value;
  const sel = base.filter(f => !PK || (PK === 'no' ? (f.pk !== 'yes' && f.pk !== 'extra') : f.pk === PK))
                  .filter(f => !CD || (CD === 'none' ? !f.cd : f.cd === CD));
  const span = diff(from, to) + 1, daily = span <= 45;

  const days = [], idx = {};
  for (let d = from; d <= to; d = addD(d, 1)) { idx[d] = days.length; days.push({d: d, active: 0, nw: 0, gone: 0}); }
  for (const f of sel) {
    const a = f.first > from ? f.first : from, b = f.last < to ? f.last : to;
    for (let d = a; d <= b; d = addD(d, 1)) days[idx[d]].active++;
    if (!f.init && idx[f.first] != null) days[idx[f.first]].nw++;
    if (f.last <= CUT) { const g = addD(f.last, 1); if (idx[g] != null) days[idx[g]].gone++; }
  }
  const goneAll = sel.filter(f => f.last <= CUT && idx[addD(f.last, 1)] != null);
  const goneF = goneAll.filter(f => !f.init);
  const durs = goneF.map(f => diff(f.first, f.last) + 1);
  const eurNow = sel.filter(f => f.area >= 15 && pr(f) && f.first <= to && f.last >= from).map(f => pr(f) / f.area);
  const act = sel.filter(f => f.first <= to && f.last >= to).length;
  const pmTxt = $('pm').value === 'rent' ? 'bez energií' : 's energiami';
  const prNow = sel.filter(f => pr(f) && f.first <= to && f.last >= from).map(pr);
  const sum = k => days.reduce((s, x) => s + x[k], 0);

  $('cap').textContent = 'Obdobie ' + from + ' až ' + to + ' (' + span + ' dní). Dáta sa zbierajú od ' + D.start + '.';
  const hasP = f => f.pk === 'yes' || f.pk === 'extra';
  const inWin = f => pr(f) && f.first <= to && f.last >= from;
  const cmp = a => a.length ? fmt(med(a)) + ' € (n=' + a.length + ')' : '–';
  $('pkcmp').textContent = 'Medián ceny za byt ' + pmTxt + ': s parkovaním ' + cmp(base.filter(f => hasP(f) && inWin(f)).map(pr)) +
    ', bez parkovania / neuvedené ' + cmp(base.filter(f => !hasP(f) && inWin(f)).map(pr)) + '.';
  $('tiles').innerHTML = [
    [act, to === T ? 'aktívnych inzerátov teraz' : 'aktívnych inzerátov k ' + to, 'act'],
    [sum('nw'), 'nových v období', 'new'],
    [sum('gone'), 'zmizlo (obsadených) v období', 'gone'],
    [durs.length ? fmt(med(durs)) + ' dní' : '–', 'medián obsadenia (n=' + durs.length + ')'],
    [prNow.length ? fmt(med(prNow)) + ' €' : '–', 'medián ceny za byt mesačne, ' + pmTxt + ' (n=' + prNow.length + ')'],
    [eurNow.length ? fmt(med(eurNow)) + ' €/m²' : '–', 'medián za m², ' + pmTxt + ' (n=' + eurNow.length + ')']
  ].map(x => x[2] ? '<button class="tile" data-w="' + x[2] + '"><b>' + x[0] + '</b><span>' + x[1] + ' ▾</span></button>'
                  : '<div class="tile"><b>' + x[0] + '</b><span>' + x[1] + '</span></div>').join('');

  // ponuka vs. dopyt: zásoba v mesiacoch = aktívne byty / tempo obsadzovania za mesiac
  const LIM = {tight: 0.75, loose: 2};   // orientačné hranice (mesiace zásoby)
  const sup = (S, a, b) => {
    const n = S.filter(f => f.first <= b && f.last >= b).length;
    let g = 0;
    for (const f of S) if (f.last <= CUT) { const e = addD(f.last, 1); if (e >= a && e <= b) g++; }
    const rate = g / (diff(a, b) + 1) * 30;
    return {n: n, g: g, rate: rate, m: g ? n / rate : null};
  };
  const cur = sup(sel, from, to);
  const f2 = addD(from, -span), t2 = addD(from, -1);
  const prev = f2 >= D.start ? sup(sel, f2, t2) : null;
  let verdict = 'Zatiaľ sa nedá určiť', pos = null, detail = 'V zvolenom období zatiaľ nezmizol žiadny inzerát, takže nevieme odhadnúť, ako rýchlo sa byty obsadzujú.';
  if (cur.m != null) {
    verdict = cur.m < LIM.tight ? 'Nedostatok bytov' : (cur.m > LIM.loose ? 'Prebytok bytov' : 'Vyvážený trh');
    pos = Math.min(cur.m, 4) / 4 * 100;
    detail = 'V ponuke je ' + cur.n + ' bytov a obsadzuje sa ich asi ' + fmt(cur.rate) + ' za 30 dní. Pri tomto tempe by ponuka vystačila na ' + fmt(cur.m) + ' mes.';
    if (cur.g < 5) detail += ' Zmizlo však len ' + cur.g + ' bytov, takže ber to ako veľmi orientačné.';
    if (prev && prev.m != null) detail += ' Oproti predchádzajúcemu rovnako dlhému obdobiu ' +
      (cur.m > prev.m * 1.15 ? 'zásoba rastie (ponuka sa nasycuje).' : (cur.m < prev.m * 0.85 ? 'zásoba klesá (trh sa uťahuje).' : 'sa nič výrazné nezmenilo.'));
  }
  $('gauge').innerHTML = '<h2>' + verdict + '</h2><p>' + detail + '</p>' +
    (pos == null ? '' : '<div class="scale"><i class="s1" style="width:18.75%"></i><i class="s2" style="width:31.25%"></i><i class="s3" style="width:50%"></i><b style="left:' + pos + '%"></b></div>' +
    '<div class="scl"><span style="width:18.75%">nedostatok</span><span style="width:31.25%">vyvážené</span><span style="width:50%">prebytok</span></div>') +
    '<p style="font-size:12px">Orientačné hranice: do ' + LIM.tight + ' mes. zásoby nedostatok, nad ' + LIM.loose + ' mes. prebytok. Počíta sa len z inzerátov na Nehnuteľnosti.sk.</p>';
  const nwS = sum('nw'), goS = sum('gone'), th = Math.max(1, Math.round(0.1 * Math.max(nwS, goS)));
  $('c2txt').textContent = 'Za obdobie pribudlo ' + nwS + ' nových inzerátov a ' + goS + ' zmizlo (byt sa obsadil). ' +
    (nwS - goS > th ? 'Ponuka rastie: byty pribúdajú rýchlejšie, než sa obsadzujú.' :
     (goS - nwS > th ? 'Ponuka klesá: byty sa obsadzujú rýchlejšie, než pribúdajú nové.' : 'Ponuka je zhruba stabilná.'));

  // zoskupenie do dní alebo týždňov
  const bk = new Map();
  for (const x of days) {
    const k = daily ? x.d : monday(x.d);
    const o = bk.get(k) || {nw: 0, gone: 0, s: x.d, e: x.d};
    o.nw += x.nw; o.gone += x.gone; o.e = x.d; bk.set(k, o);
  }
  const keys = [...bk.keys()];
  make('c1', 'b1', 'line', days.map(x => x.d), [{c: '--s1', o: {label: 'Aktívne', data: days.map(x => x.active)}}], 2, false);
  make('c2', 'b2', 'bar', keys, [{c: '--s1', o: {label: 'Nové inzeráty', data: keys.map(k => bk.get(k).nw)}},
       {c: '--s2', o: {label: 'Zmizli (obsadené)', data: keys.map(k => bk.get(k).gone)}}], 1, true);
  const byLay = {};
  goneF.forEach(f => (byLay[f.lay] = byLay[f.lay] || []).push(diff(f.first, f.last) + 1));
  const ls = Object.keys(byLay).sort((a, b) => a.localeCompare(b, 'sk', {numeric: true}));
  make('c3', 'b3', 'bar', ls.map(l => l + ' (n=' + byLay[l].length + ')'),
       [{c: '--s1', o: {label: 'Medián dní', data: ls.map(l => med(byLay[l]))}}], 1, false);
  const eurs = keys.map(k => { const o = bk.get(k);
    const v = sel.filter(f => f.area >= 15 && pr(f) && f.first <= o.e && f.last >= o.s).map(f => pr(f) / f.area);
    return v.length ? Math.round(med(v) * 100) / 100 : null; });
  make('c4', 'b4', 'line', keys, [{c: '--s1', o: {label: '€/m²', data: eurs}}], 2, false);

  document.querySelector('#tbl tbody').innerHTML = days.slice().reverse().slice(0, 90).map(x =>
    '<tr><td>' + x.d + '</td><td>' + x.active + '</td><td>' + x.nw + '</td><td>' + x.gone + '</td></tr>').join('');

  const view = document.querySelector('input[name=w]:checked').value, gone = view === 'gone';
  let rows;
  if (gone) rows = goneAll.slice().sort((a, b) => b.last.localeCompare(a.last));
  else {
    rows = view === 'new' ? sel.filter(f => !f.init && f.first >= from && f.first <= to)
                          : sel.filter(f => f.first <= to && f.last >= to);
    rows = rows.sort((a, b) => b.first.localeCompare(a.first));
  }
  $('dh').textContent = gone ? 'Naposledy videný' : 'Od';
  $('lst').querySelector('tbody').innerHTML = rows.length ? rows.map(r => {
    const ok = /^https:\/\//.test(r.url);
    const link = ok ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener" title="' + esc(r.snap) + '">' : '';
    return '<tr><td>' + (ok ? link + esc(r.lay) + '</a>' : esc(r.lay)) + '</td><td class="l">' + esc(r.street) + '</td><td>' +
      esc(r.area == null ? '' : r.area) + '</td><td>' + (r.p ? esc(r.p) + ' €' : '') + '</td><td>' + esc(enTxt(r)) + '</td><td>' +
      (total(r) ? esc(total(r)) + ' €' : '–') + '</td><td>' + esc(pkTxt(r)) + '</td><td>' + esc(cdTxt(r)) + '</td><td>' +
      (gone ? r.last : r.first) + '</td><td>' + (ok ? link + 'otvoriť</a>' : '') + '</td></tr>';
  }).join('') : '<tr><td colspan="10">Žiadne inzeráty.</td></tr>';

  const EVT = {new: 'Nový inzerát', gone: 'Zmizol z ponuky', rented: 'Potvrdený ako obsadený', price: 'Zmena ceny',
               relist: 'Obnovený inzerát (nové ID)', back: 'Znovu v ponuke'};
  const selSet = new Set(sel);
  renderMerged();
  const evs = D.events.filter(e => e[0] >= from && e[0] <= to && selSet.has(F[e[2]]));
  $('evn').textContent = evs.length;
  $('evt').querySelector('tbody').innerHTML = evs.length ? evs.map(e => {
    const f = F[e[2]], ok = /^https:\/\//.test(f.url);
    const name = esc(f.lay) + ' · ' + esc(f.street || '—') + ' · ' + (f.area == null ? '?' : esc(f.area)) + ' m² · ' + (f.p ? esc(f.p) + ' €' : '?');
    return '<tr><td>' + e[0] + '</td><td class="l">' + esc(EVT[e[1]] || e[1]) + '</td><td class="l">' +
      (ok ? '<a href="' + esc(f.url) + '" target="_blank" rel="noopener" title="' + esc(f.snap) + '">' + name + '</a>' : name) +
      '</td><td class="l">' + esc(e[3]) + '</td></tr>';
  }).join('') : '<tr><td colspan="4">Žiadne udalosti v tomto období.</td></tr>';

  const now = sel.filter(f => f.last === T), unk = now.filter(f => !pr(f)).length;
  const m = D.merges;
  $('note').textContent = 'Za „obsadený“ sa berie byt, ktorého inzerát zmizol aspoň ' + D.gone_after +
    ' dní. Byty videné hneď pri prvom behu sa do doby obsadenia a do „nových“ nerátajú (nevieme, odkedy visia). ' +
    'Ak sa ten istý byt objaví pod novým inzerátom, spája sa podľa dispozície, m², ulice, ceny a textu popisu' +
    (m.n ? ' (zatiaľ ' + m.n + ' spojení, z toho menej istých ' + m.weak + ')' : '') +
    '. Nájom aj energie sa čítajú z textu inzerátu. „Celkom“ = nájom + energie (alebo nájom, ak sú v cene). Do cien sa započítavajú len inzeráty s jasne uvedenými energiami; pri ' + unk + ' z ' + now.length + ' aktívnych inzerátov sa cena vo zvolenom režime určiť nedala (stĺpec Energie v zozname). Parkovanie sa tiež číta z textu inzerátu; byty s parkovaním bývajú aj novšie či väčšie, preto rozdiel v cene nie je len „za parkovanie“. Čísla budú spoľahlivé po niekoľkých týždňoch zberu.';
}

$('per').addEventListener('change', () => { $('cust').hidden = $('per').value !== 'custom'; store.set('per', $('per').value); refresh(); });
$('lay').addEventListener('change', () => { store.set('lay', $('lay').value); refresh(); });
$('pkf').addEventListener('change', refresh);
$('cdf').addEventListener('change', refresh);
$('tiles').addEventListener('click', e => {
  const b = e.target.closest('[data-w]'); if (!b) return;
  document.querySelector('input[name=w][value=' + b.dataset.w + ']').checked = true;
  $('listbox').open = true; refresh();
  $('listbox').scrollIntoView({behavior: 'smooth', block: 'start'});
});
$('pm').addEventListener('change', () => { store.set('pm', $('pm').value); refresh(); });
[$('d1'), $('d2')].forEach(i => i.addEventListener('change', refresh));
document.querySelectorAll('input[name=w]').forEach(i => i.addEventListener('change', refresh));
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', refresh);
$('cust').hidden = $('per').value !== 'custom';
refresh();
</script></body></html>
"""


def build_site(data):
    SITE.mkdir(exist_ok=True)
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (SITE / "index.html").write_text(PAGE.replace("__DATA__", blob), encoding="utf-8")


# ------------------------------------------------------------------------ main
def main():
    tz = ZoneInfo("Europe/Bratislava")
    today = (date.fromisoformat(os.environ["TRACKER_TODAY"])
             if os.environ.get("TRACKER_TODAY") else datetime.now(tz).date())
    ex = load_exclude()
    purge(ex)
    items = [i for i in scrape() if i["id"] not in ex]
    if not items:
        sys.exit("CHYBA: nenašiel som žiadne inzeráty (blokovanie alebo zmena stránky). Dáta som nezmenil.")
    enrich(items, today)
    prev_daily = read_csv(DATA / "daily.csv")
    prev_active = int(prev_daily[-1]["active"]) if prev_daily and prev_daily[-1]["date"] != today.isoformat() else 0
    if prev_active and len(items) < 0.5 * prev_active:
        sys.exit(f"CHYBA: našlo sa len {len(items)} inzerátov oproti {prev_active} včera. Dáta som nezmenil.")
    flats, daily, events = update(items, today)
    all_events = finish_events(flats, events, today)
    build_site(build_data(flats, daily, today, all_events))
    print(f"OK {today}: {len(items)} inzerátov, {daily[-1]['active']} aktívnych bytov, "
          f"preskočených (iná lokalita): {SKIPPED[0]}")


if __name__ == "__main__":
    main()