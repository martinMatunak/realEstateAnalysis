#!/usr/bin/env python3
"""Sledovanie prenájmov bytov v Devínskej Novej Vsi.

Raz denne: stiahne výpisy z Nehnuteľnosti.sk, aktualizuje CSV v priečinku data/
a vygeneruje stránku docs/index.html. Spustenie: python tracker.py
"""
import csv
import json
import os
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta
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
REMATCH_DAYS = 14        # nový inzerát s rovnakým odtlačkom do tolkých dní = ten istý byt
DATA = Path("data")
SITE = Path("docs")
DEBUG = Path("debug")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124 Safari/537.36",
           "Accept-Language": "sk-SK,sk;q=0.9"}
FLAT_FIELDS = ["flat_id", "ids", "fp", "street", "layout", "area", "first_seen",
               "last_seen", "price_first", "price_last", "status", "initial", "url", "title"]
DAILY_FIELDS = ["date", "active", "new", "gone"]
PRICE_FIELDS = ["date", "flat_id", "price"]
ID_RE = re.compile(r"/detail/([A-Za-z0-9]+)")


# ---------------------------------------------------------------- stiahnutie
def fetch(kind, page):
    fixture = os.environ.get("TRACKER_FIXTURE_DIR")
    if fixture:
        p = Path(fixture) / f"{kind}.html"
        return p.read_text(encoding="utf-8") if (p.exists() and page == 1) else None
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
    m = re.search(rf"((?:[{U}][{L}.\-]+\s+){{0,2}}[{U}][{L}.\-]+(?:\s+\d+[A-Za-z/]*)?)"
                  r"\s*,\s*(?:Bratislava\s*-\s*)?Devínska Nová Ves", text)
    if m:
        street = m.group(1).strip()
    head = text[:120].lower()
    status = "REZERVOVANÉ" if "rezervovan" in head else ("PRENAJATÉ" if "prenajat" in head else "")
    return {"id": lid, "price": price, "area": area, "layout": layout,
            "street": street, "status": status}


def score(it):
    return sum(it[k] not in (None, "") for k in ("price", "area", "layout", "street"))


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    found, urls, titles = {}, {}, {}
    for a in soup.find_all("a", href=ID_RE):
        lid = ID_RE.search(a["href"]).group(1)
        href = a["href"].split("?")[0].split("#")[0]
        urls.setdefault(lid, BASE + href if href.startswith("/") else href)
        label = " ".join(a.get_text(" ", strip=True).split())
        if len(label) > len(titles.get(lid, "")):
            titles[lid] = label[:100]
        node = a
        # kartu = najväčší predok, ktorý obsahuje odkazy len na jeden inzerát
        while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
            ids = {ID_RE.search(x["href"]).group(1)
                   for x in node.parent.find_all("a", href=ID_RE)}
            if len(ids) > 1:
                break
            node = node.parent
        text = " ".join(node.get_text(" ", strip=True).split())
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
        for page in range(1, MAX_PAGES + 1):
            html = fetch(kind, page)
            if not html:
                break
            raw.append(html)
            got = parse(html)
            new_ids = {i["id"] for i in got} - seen_before
            seen_before |= new_ids
            for it in got:
                if it["id"] not in items or score(it) > score(items[it["id"]]):
                    items[it["id"]] = it
            if not new_ids or len(got) < 15:
                break
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


def fingerprint(it):
    # odtlačok bytu: layout + plocha + ulica s číslom domu (cena a text sa nemenia)
    m = re.search(r"([^\W\d_]+)\s+(\d+[A-Za-z/]*)\s*$", it["street"])
    if it["layout"] and it["area"] and m:   # posledné slovo ulice + číslo domu
        return f"{it['layout']}|{round(it['area'])}|{m.group(1).lower()} {m.group(2).lower()}"
    return ""


def update(items, today):
    t = today.isoformat()
    flats = read_csv(DATA / "listings.csv")
    daily = [r for r in read_csv(DATA / "daily.csv") if r["date"] != t]
    prices = [r for r in read_csv(DATA / "prices.csv") if r["date"] != t]
    first_run = not flats
    by_id = {}
    for f in flats:
        for i in f["ids"].split("|"):
            by_id[i] = f
    prev = max((r["date"] for r in daily), default=None)

    today_flats = {}   # flat_id -> (flat row, min price)
    created = 0
    for it in items:
        fl = by_id.get(it["id"])
        fp = fingerprint(it)
        if fl is None and fp:   # rovnaký byt, nové ID inzerátu
            for c in flats:
                if (c["fp"] == fp and c["last_seen"] < t and
                        (today - date.fromisoformat(c["last_seen"])).days <= REMATCH_DAYS):
                    fl = c
                    fl["ids"] += "|" + it["id"]
                    by_id[it["id"]] = fl
                    break
        if fl is None:
            fl = {"flat_id": it["id"], "ids": it["id"], "fp": fp, "street": it["street"],
                  "layout": it["layout"] or "", "area": it["area"] or "",
                  "first_seen": t, "last_seen": t,
                  "price_first": it["price"] or "", "price_last": it["price"] or "",
                  "status": it["status"], "initial": "1" if first_run else "0",
                  "url": it.get("url", ""), "title": it.get("title", "")}
            flats.append(fl)
            by_id[it["id"]] = fl
            created += 1
            if it["price"]:
                prices.append({"date": t, "flat_id": fl["flat_id"], "price": it["price"]})
        cur = today_flats.get(fl["flat_id"])
        price = it["price"]
        if cur is None:
            today_flats[fl["flat_id"]] = (fl, price, it)
        elif price and (cur[1] is None or price < cur[1]):
            today_flats[fl["flat_id"]] = (fl, price, it)

    for fl, price, it in today_flats.values():
        if fl["last_seen"] != t or fl["first_seen"] != t:
            if price and str(price) != str(fl["price_last"]):
                prices.append({"date": t, "flat_id": fl["flat_id"], "price": price})
        fl["last_seen"] = t
        if price:
            fl["price_last"] = price
            if not fl["price_first"]:
                fl["price_first"] = price
        fl["status"] = it["status"]
        if it.get("url"):
            fl["url"], fl["title"] = it["url"], it.get("title", "")
        for k in ("street", "layout", "area"):
            if not fl[k] and it[k]:
                fl[k] = it[k]
        if not fl["fp"]:
            fl["fp"] = fingerprint(it)

    gone = sum(1 for f in flats if prev and f["last_seen"] == prev and f["last_seen"] != t)
    daily.append({"date": t, "active": len(today_flats), "new": 0 if first_run else created, "gone": gone})
    daily.sort(key=lambda r: r["date"])
    write_csv(DATA / "listings.csv", FLAT_FIELDS, flats)
    write_csv(DATA / "daily.csv", DAILY_FIELDS, daily)
    write_csv(DATA / "prices.csv", PRICE_FIELDS, prices)
    return flats, daily


# ------------------------------------------------------------------ štatistiky
def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def week_start(d):
    d = date.fromisoformat(d)
    return (d - timedelta(days=d.weekday())).isoformat()


def layout_label(l):
    return "Garsónka / 1i" if l == "1" else f"{l}i"


def analyse(flats, daily, today):
    t = today.isoformat()
    cutoff = (today - timedelta(days=GONE_AFTER_DAYS)).isoformat()
    d30 = (today - timedelta(days=30)).isoformat()
    d7 = (today - timedelta(days=7)).isoformat()

    active = sum(1 for f in flats if f["last_seen"] == t)
    new7 = sum(1 for f in flats if f["first_seen"] >= d7 and f["initial"] == "0")
    rented30 = sum(1 for f in flats if d30 <= f["last_seen"] <= cutoff)

    durations = {}
    for f in flats:
        if f["last_seen"] <= cutoff and f["initial"] == "0":
            days = (date.fromisoformat(f["last_seen"]) - date.fromisoformat(f["first_seen"])).days + 1
            durations.setdefault(f["layout"] or "?", []).append(days)
    all_d = [d for v in durations.values() for d in v]
    median_days = statistics.median(all_d) if all_d else None

    weeks = {}
    for r in daily:
        w = weeks.setdefault(week_start(r["date"]), [0, 0])
        w[0] += int(r["new"])
        w[1] += int(r["gone"])

    eur = {}
    for f in flats:
        p, a = num(f["price_first"]), num(f["area"])
        if p and a and a >= 15:
            eur.setdefault(week_start(f["first_seen"]), []).append(p / a)

    def row(f):
        return {"url": f.get("url", ""), "layout": layout_label(f["layout"]) if f["layout"] else "?",
                "street": f["street"], "area": num(f["area"]), "price": num(f["price_last"]),
                "first": f["first_seen"], "last": f["last_seen"], "title": f.get("title", "")}
    listing = sorted((row(f) for f in flats if f["last_seen"] == t), key=lambda r: r["first"], reverse=True)
    gone_list = sorted((row(f) for f in flats if d30 <= f["last_seen"] <= cutoff),
                       key=lambda r: r["last"], reverse=True)

    return {
        "list": listing, "gone": gone_list,
        "updated": t,
        "tiles": {"active": active, "new7": new7, "rented30": rented30,
                  "median_days": median_days, "n_durations": len(all_d)},
        "active": {"labels": [r["date"] for r in daily], "values": [int(r["active"]) for r in daily]},
        "weekly": {"labels": sorted(weeks), "new": [weeks[w][0] for w in sorted(weeks)],
                   "gone": [weeks[w][1] for w in sorted(weeks)]},
        "duration": {"labels": [f"{layout_label(l)} (n={len(durations[l])})" for l in sorted(durations)],
                     "values": [statistics.median(durations[l]) for l in sorted(durations)]},
        "eur": {"labels": sorted(eur), "values": [round(statistics.median(eur[w]), 2) for w in sorted(eur)]},
        "table": daily[-30:][::-1],
    }


PAGE = r"""<!doctype html>
<html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DNV prenájmy</title>
<style>
:root{color-scheme:light;--bg:#fcfcfb;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--line:rgba(11,11,11,.12);--s1:#2a78d6;--s2:#eb6834}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#1a1a19;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:rgba(240,239,236,.14);--s1:#3987e5;--s2:#d95926}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--ink2);font-size:14px;margin:0 0 18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.tile{border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:26px;font-weight:600}.tile span{color:var(--ink2);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:16px}
@media(max-width:480px){.grid{grid-template-columns:1fr}}
.card{border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{font-size:15px;margin:0 0 2px}.card p{margin:0 0 10px;color:var(--ink2);font-size:13px}
.box{position:relative;height:240px}.empty{color:var(--ink2);font-size:14px;padding:70px 0;text-align:center}
details{margin-top:20px}summary{cursor:pointer;color:var(--ink2)}
table{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}
td,th{border-bottom:1px solid var(--line);padding:4px 8px;text-align:right}th:first-child,td:first-child{text-align:left}
.note{color:var(--ink2);font-size:13px;margin-top:18px}
.bar{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-bottom:8px;font-size:14px}
select{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
a{color:var(--s1)}.scroll{overflow-x:auto}
</style></head><body><main>
<h1>Prenájmy bytov v Devínskej Novej Vsi</h1>
<p class="sub" id="upd"></p>
<div class="tiles" id="tiles"></div>
<div class="grid">
<div class="card"><h2>Aktívne inzeráty</h2><p>Počet bytov v ponuke v daný deň</p><div class="box" id="b1"><canvas id="c1"></canvas></div></div>
<div class="card"><h2>Nové a zmiznuté za týždeň</h2><p>Rastie ponuka (nové &gt; zmiznuté) alebo klesá?</p><div class="box" id="b2"><canvas id="c2"></canvas></div></div>
<div class="card"><h2>Medián dní do obsadenia</h2><p>Podľa dispozície; len byty, ktoré sme videli pribudnúť aj zmiznúť</p><div class="box" id="b3"><canvas id="c3"></canvas></div></div>
<div class="card"><h2>Medián nájmu za m²</h2><p>€/m² pri prvom zverejnení, po týždňoch</p><div class="box" id="b4"><canvas id="c4"></canvas></div></div>
</div>
<div class="card" style="margin-top:16px"><h2>Inzeráty</h2><p>Klikni na dispozíciu alebo „otvoriť“, inzerát sa otvorí v novom okne.</p>
<div class="bar"><select id="fl" aria-label="Dispozícia"></select>
<label><input type="radio" name="w" value="act" checked> Aktívne</label>
<label><input type="radio" name="w" value="gone"> Zmizli za 30 dní</label></div>
<div class="scroll"><table id="lst"><thead><tr><th>Dispozícia</th><th>Ulica</th><th>m²</th><th>Nájom</th><th id="dh">Od</th><th></th></tr></thead><tbody></tbody></table></div></div>
<details><summary>Tabuľka posledných dní</summary>
<table id="tbl"><thead><tr><th>Dátum</th><th>Aktívne</th><th>Nové</th><th>Zmizli</th></tr></thead><tbody></tbody></table></details>
<p class="note">Za „obsadený“ sa berie byt, ktorého inzerát zmizol aspoň 7 dní. Byty videné hneď pri prvom behu sa do doby obsadenia nerátajú (nevieme, odkedy visia). Cena je nájom z inzerátu, energie bývajú zvlášť alebo v cene podľa inzerátu. Čísla budú spoľahlivé po niekoľkých týždňoch až mesiacoch zberu.</p>
</main>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
const D = __DATA__;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
let charts = [];
document.getElementById('upd').textContent = 'Aktualizované ' + D.updated;
const T = D.tiles, md = T.median_days;
document.getElementById('tiles').innerHTML = [
  [T.active, 'aktívnych inzerátov'], [T.new7, 'nových za 7 dní'], [T.rented30, 'obsadených za 30 dní'],
  [md === null ? '–' : md + ' dní', md === null ? 'medián obsadenia (zatiaľ málo dát)' : 'medián obsadenia (n=' + T.n_durations + ')']
].map(x => '<div class="tile"><b>' + x[0] + '</b><span>' + x[1] + '</span></div>').join('');
document.querySelector('#tbl tbody').innerHTML = D.table.map(r =>
  '<tr><td>' + r.date + '</td><td>' + r.active + '</td><td>' + r.new + '</td><td>' + r.gone + '</td></tr>').join('');

function make(id, boxId, type, labels, sets, minPoints, legend) {
  const box = document.getElementById(boxId);
  if (labels.length < minPoints) { box.innerHTML = '<div class="empty">Zatiaľ málo dát, graf sa zaplní po pár dňoch.</div>'; return; }
  const ink2 = css('--ink2'), line = css('--line');
  charts.push(new Chart(document.getElementById(id), {
    type: type,
    data: {labels: labels, datasets: sets.map(s => Object.assign({
      borderColor: css(s.c), backgroundColor: css(s.c), borderWidth: 2, tension: 0,
      pointRadius: 0, pointHoverRadius: 5, borderRadius: type === 'bar' ? {topLeft: 4, topRight: 4} : 0,
      borderSkipped: 'bottom', maxBarThickness: 28}, s.o))},
    options: {responsive: true, maintainAspectRatio: false,
      interaction: {mode: 'index', intersect: false},
      plugins: {legend: {display: legend, labels: {color: ink2, boxWidth: 12, boxHeight: 12}}},
      scales: {x: {ticks: {color: ink2, maxRotation: 0, autoSkip: true}, grid: {display: false}, border: {color: line}},
               y: {beginAtZero: true, ticks: {color: ink2}, grid: {color: line}, border: {display: false}}}}
  }));
}
function render() {
  charts.forEach(c => c.destroy()); charts = [];
  make('c1', 'b1', 'line', D.active.labels, [{c: '--s1', o: {label: 'Aktívne', data: D.active.values}}], 2, false);
  make('c2', 'b2', 'bar', D.weekly.labels, [{c: '--s1', o: {label: 'Nové', data: D.weekly.new}},
       {c: '--s2', o: {label: 'Zmizli', data: D.weekly.gone}}], 1, true);
  make('c3', 'b3', 'bar', D.duration.labels, [{c: '--s1', o: {label: 'Medián dní', data: D.duration.values}}], 1, false);
  make('c4', 'b4', 'line', D.eur.labels, [{c: '--s1', o: {label: '€/m²', data: D.eur.values}}], 2, false);
}
const esc = x => String(x == null ? '' : x).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const fl = document.getElementById('fl');
const kinds = [...new Set(D.list.concat(D.gone).map(r => r.layout))].sort();
fl.innerHTML = '<option value="">Všetky dispozície</option>' + kinds.map(k => '<option>' + esc(k) + '</option>').join('');
function lst() {
  const gone = document.querySelector('input[name=w]:checked').value === 'gone';
  const f = fl.value, rows = (gone ? D.gone : D.list).filter(r => !f || r.layout === f);
  document.getElementById('dh').textContent = gone ? 'Naposledy videný' : 'Od';
  document.querySelector('#lst tbody').innerHTML = rows.length ? rows.map(r => {
    const ok = /^https:\/\//.test(r.url);
    const a = ok ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.layout) + '</a>' : esc(r.layout);
    return '<tr><td>' + a + '</td><td>' + esc(r.street) + '</td><td>' + esc(r.area) + '</td><td>' +
      (r.price ? esc(r.price) + ' €' : '') + '</td><td>' + (gone ? r.last : r.first) + '</td><td>' +
      (ok ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener">otvoriť</a>' : '') + '</td></tr>';
  }).join('') : '<tr><td colspan="6">Žiadne inzeráty.</td></tr>';
}
fl.addEventListener('change', lst);
document.querySelectorAll('input[name=w]').forEach(i => i.addEventListener('change', lst));
lst();
render();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { render(); });
</script></body></html>
"""


def build_site(stats):
    SITE.mkdir(exist_ok=True)
    html = PAGE.replace("__DATA__", json.dumps(stats, ensure_ascii=False))
    (SITE / "index.html").write_text(html, encoding="utf-8")


# ------------------------------------------------------------------------ main
def main():
    tz = ZoneInfo("Europe/Bratislava")
    today = (date.fromisoformat(os.environ["TRACKER_TODAY"])
             if os.environ.get("TRACKER_TODAY") else datetime.now(tz).date())
    items = scrape()
    if not items:
        sys.exit("CHYBA: nenašiel som žiadne inzeráty (blokovanie alebo zmena stránky). Dáta som nezmenil.")
    prev_daily = read_csv(DATA / "daily.csv")
    prev_active = int(prev_daily[-1]["active"]) if prev_daily and prev_daily[-1]["date"] != today.isoformat() else 0
    if prev_active and len(items) < 0.5 * prev_active:
        sys.exit(f"CHYBA: našlo sa len {len(items)} inzerátov oproti {prev_active} včera. Dáta som nezmenil.")
    flats, daily = update(items, today)
    build_site(analyse(flats, daily, today))
    print(f"OK {today}: {len(items)} inzerátov, {daily[-1]['active']} aktívnych bytov")


if __name__ == "__main__":
    main()