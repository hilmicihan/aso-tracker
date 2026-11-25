"""data/raw/* dosyalarından SQLite veritabanını kurar ve dashboard verisini üretir.

Çıktılar:
  data/rankings.db        tüm geçmiş, sorgulamaya hazır (git'e eklenmez, her seferinde yeniden kurulur)
  site/data/data.json     dashboard'un okuduğu özet
  site/rankings.db        veritabanının indirilebilir kopyası

Kullanım:  python -m tracker.build
"""
import csv
import datetime as dt
import gzip
import json
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
POP = ROOT / "data" / "popularity"   # elle eklenen popülerlik ölçümleri: <YYYY-MM-DD>.csv
DB = ROOT / "data" / "rankings.db"
SITE_DATA = ROOT / "site" / "data"

# Hangi mağazada hangi dildeki metadata geçerli (olay akışında gösterilecek diller)
COUNTRY_LOCALES = {
    "us": ["en-US", "es-MX"], "tr": ["tr"], "gb": ["en-GB"], "de": ["de-DE"], "fr": ["fr-FR"],
    "br": ["pt-BR"], "jp": ["ja"], "es": ["es-ES"], "it": ["it"], "ca": ["en-CA", "fr-CA"],
    "au": ["en-AU"], "mx": ["es-MX"], "nl": ["nl-NL"], "kr": ["ko"],
}
LIVE = "READY_FOR_DISTRIBUTION"

SCHEMA = """
CREATE TABLE keywords (id INTEGER PRIMARY KEY, country TEXT NOT NULL, term TEXT NOT NULL, UNIQUE(country, term));
CREATE TABLE apps (app_id INTEGER PRIMARY KEY, name TEXT, developer TEXT, is_mine INTEGER DEFAULT 0,
                   short_name TEXT, first_seen TEXT);
CREATE TABLE targets (date TEXT, keyword_id INTEGER, app_id INTEGER, PRIMARY KEY (date, keyword_id, app_id));
CREATE TABLE rankings (date TEXT, keyword_id INTEGER, app_id INTEGER, rank INTEGER,
                       PRIMARY KEY (date, keyword_id, app_id));
CREATE TABLE app_snapshots (date TEXT, country TEXT, app_id INTEGER, name TEXT, developer TEXT, version TEXT,
                            version_date TEXT, rating REAL, rating_count INTEGER, price REAL, genre TEXT,
                            PRIMARY KEY (date, country, app_id));
CREATE TABLE metadata (date TEXT, app_id INTEGER, locale TEXT, state TEXT, name TEXT, subtitle TEXT, keywords TEXT,
                       PRIMARY KEY (date, app_id, locale, state));
CREATE TABLE runs (date TEXT PRIMARY KEY, status TEXT, duration_s INTEGER, failed_queries INTEGER, errors TEXT);
CREATE TABLE popularity (date TEXT, keyword_id INTEGER, popularity INTEGER, difficulty INTEGER, ranked_apps INTEGER,
                         source TEXT, PRIMARY KEY (date, keyword_id, source));
CREATE INDEX ix_rank_kw ON rankings(keyword_id, date);
CREATE INDEX ix_rank_app ON rankings(app_id, date);
"""


def read_csv_gz(path):
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_db():
    tmp = DB.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    kw_ids = {}

    def kw_id(country, term):
        key = (country, term)
        if key not in kw_ids:
            cur = con.execute("INSERT INTO keywords(country, term) VALUES (?, ?)", key)
            kw_ids[key] = cur.lastrowid
        return kw_ids[key]

    dates = sorted(p.name for p in RAW.iterdir() if p.is_dir() and (p / "rankings.csv.gz").exists())
    for date in dates:
        d = RAW / date
        for r in csv.DictReader(open(d / "keywords.csv", encoding="utf-8")):
            aid = int(r["app_id"])
            con.execute("INSERT OR IGNORE INTO apps(app_id, is_mine, short_name, first_seen) VALUES (?,1,?,?)",
                        (aid, r["app"], date))
            con.execute("UPDATE apps SET is_mine=1, short_name=? WHERE app_id=?", (r["app"], aid))
            con.execute("INSERT OR IGNORE INTO targets VALUES (?,?,?)",
                        (date, kw_id(r["country"].strip().lower(), r["term"].strip()), aid))
        con.executemany("INSERT OR REPLACE INTO rankings VALUES (?,?,?,?)",
                        [(date, kw_id(r["country"], r["term"]), int(r["app_id"]), int(r["rank"]))
                         for r in read_csv_gz(d / "rankings.csv.gz")])
        for r in read_csv_gz(d / "apps.csv.gz"):
            aid = int(r["app_id"])
            con.execute("INSERT OR IGNORE INTO apps(app_id, first_seen) VALUES (?,?)", (aid, date))
            con.execute("UPDATE apps SET name=?, developer=? WHERE app_id=?", (r["name"], r["developer"], aid))
            con.execute("INSERT OR REPLACE INTO app_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (date, r["country"], aid, r["name"], r["developer"], r["version"], r["version_date"],
                         float(r["rating"] or 0), int(r["rating_count"] or 0), float(r["price"] or 0), r["genre"]))
        meta_path = d / "metadata.json.gz"
        if meta_path.exists():
            with gzip.open(meta_path, "rt", encoding="utf-8") as f:
                for app in json.load(f):
                    aid = int(app["app_id"])
                    for locale, states in app["info_localizations"].items():
                        kws = {}  # sürümler yeniden eskiye sıralı; her durum için en yeni sürümü al
                        for k, v in app["keywords_by_locale"].get(locale, {}).items():
                            kws.setdefault(k.split(" (")[1].rstrip(")"), v)
                        for state, info in states.items():
                            con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?,?,?,?,?,?)",
                                        (date, aid, locale, state, info.get("name"), info.get("subtitle"), kws.get(state)))
        run = json.loads((d / "run.json").read_text(encoding="utf-8")) if (d / "run.json").exists() else {}
        con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                    (date, run.get("status"), run.get("duration_s"), run.get("failed_queries"),
                     json.dumps(run.get("errors", []), ensure_ascii=False)))

    # Popülerlik (ör. Mobile Action Keyword Inspector'dan elle/yarı otomatik alınan ölçümler)
    def as_int(v):
        return int(v) if str(v).strip().isdigit() else None
    for path in sorted(POP.glob("*.csv")) if POP.exists() else []:
        for r in csv.DictReader(open(path, encoding="utf-8")):
            con.execute("INSERT OR REPLACE INTO popularity VALUES (?,?,?,?,?,?)",
                        (path.stem, kw_id(r["country"].strip().lower(), r["term"].strip()), as_int(r["popularity"]),
                         as_int(r.get("difficulty")), as_int(r.get("ranked_apps")), r.get("source") or "manual"))
    con.commit()
    con.close()
    tmp.replace(DB)
    return dates


def export_dashboard(dates):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    if not dates:
        return
    last, idx = dates[-1], {d: i for i, d in enumerate(dates)}
    mine = {r["app_id"]: r["short_name"] or r["name"] for r in q("SELECT * FROM apps WHERE is_mine=1")}
    names = {r["app_id"]: r["name"] for r in q("SELECT app_id, name FROM apps")}

    # Seriler: hedeflenen her (uygulama, ülke, ifade) + hedeflenmeden ilk 200'de görülen bizimkiler
    targeted = {(r["keyword_id"], r["app_id"]) for r in q("SELECT DISTINCT keyword_id, app_id FROM targets")}
    seen = {(r["keyword_id"], r["app_id"]) for r in
            q("SELECT DISTINCT keyword_id, app_id FROM rankings WHERE app_id IN (SELECT app_id FROM apps WHERE is_mine=1)")}
    measured = defaultdict(set)  # keyword_id -> ölçüm yapılan tarihler
    for r in q("SELECT DISTINCT keyword_id, date FROM rankings"):
        measured[r["keyword_id"]].add(r["date"])
    kws = {r["id"]: (r["country"], r["term"]) for r in q("SELECT * FROM keywords")}
    ranks = defaultdict(dict)
    for r in q("SELECT keyword_id, app_id, date, rank FROM rankings WHERE app_id IN (SELECT app_id FROM apps WHERE is_mine=1)"):
        ranks[(r["keyword_id"], r["app_id"])][r["date"]] = r["rank"]
    current_targets = {(r["keyword_id"], r["app_id"]) for r in q("SELECT keyword_id, app_id FROM targets WHERE date=?", last)}

    # Popülerlik: her ifade için en son ölçüm + geçmiş
    pop_hist = defaultdict(list)
    for r in q("SELECT * FROM popularity ORDER BY date"):
        pop_hist[r["keyword_id"]].append({"date": r["date"], "pop": r["popularity"], "diff": r["difficulty"],
                                          "apps": r["ranked_apps"], "src": r["source"]})

    series = []
    for kid, aid in sorted(targeted | seen, key=lambda k: (mine.get(k[1], ""), kws[k[0]])):
        country, term = kws[kid]
        # null = ilk 200'de yok, -1 = o gün ölçülmedi
        vals = [ranks[(kid, aid)].get(d) if d in measured[kid] else -1 for d in dates]
        p = pop_hist[kid][-1] if pop_hist[kid] else {}
        series.append({"kid": kid, "app_id": aid, "app": mine.get(aid, str(aid)), "country": country, "term": term,
                       "targeted": (kid, aid) in targeted, "active": (kid, aid) in current_targets, "ranks": vals,
                       "pop": p.get("pop"), "diff": p.get("diff"), "pop_date": p.get("date")})

    # Her ifade için son günün ilk 10'u ve bir önceki günle kıyas
    prev = dates[-2] if len(dates) > 1 else None
    snap = {(r["country"], r["app_id"]): dict(r) for r in q("SELECT * FROM app_snapshots WHERE date=?", last)}
    top = {}
    for kid, (country, term) in kws.items():
        rows = q("SELECT app_id, rank FROM rankings WHERE date=? AND keyword_id=? AND rank<=10 ORDER BY rank", last, kid)
        if not rows:
            continue
        prev_rank = {}
        if prev:
            prev_rank = {r["app_id"]: r["rank"] for r in
                         q("SELECT app_id, rank FROM rankings WHERE date=? AND keyword_id=?", prev, kid)}
        top[kid] = [{"app_id": r["app_id"], "name": names.get(r["app_id"]), "rank": r["rank"],
                     "prev": prev_rank.get(r["app_id"]) if prev else None,
                     "dev": (snap.get((country, r["app_id"])) or {}).get("developer"),
                     "ratings": (snap.get((country, r["app_id"])) or {}).get("rating_count"),
                     "mine": r["app_id"] in mine} for r in rows]

    # Olay akışı
    events = []
    tracked_countries = {c for c, _ in kws.values()}
    locales = {l for c in tracked_countries for l in COUNTRY_LOCALES.get(c, [])}
    meta_by_date = defaultdict(dict)
    for r in q("SELECT * FROM metadata WHERE state=?", LIVE):
        if r["locale"] in locales:
            meta_by_date[r["date"]][(r["app_id"], r["locale"])] = r
    for a, b in zip(dates, dates[1:]):
        for key, new in meta_by_date[b].items():
            old = meta_by_date[a].get(key)
            if not old:
                continue
            for field, label in (("name", "isim"), ("subtitle", "alt başlık"), ("keywords", "keyword'ler")):
                if (old[field] or "") != (new[field] or ""):
                    events.append({"date": b, "kind": "meta", "app": mine.get(key[0], str(key[0])),
                                   "text": f"[{key[1]}] {label}: “{old[field] or '—'}” → “{new[field] or '—'}”"})
        snaps_a = {(r["country"], r["app_id"]): r for r in q("SELECT * FROM app_snapshots WHERE date=?", a)}
        for r in q("SELECT * FROM app_snapshots WHERE date=?", b):
            o = snaps_a.get((r["country"], r["app_id"]))
            if not o:
                continue
            who = mine.get(r["app_id"]) or r["name"]
            kind = "mine" if r["app_id"] in mine else "rival"
            if o["version"] != r["version"]:
                events.append({"date": b, "kind": kind, "app": who,
                               "text": f"[{r['country']}] yeni sürüm {o['version']} → {r['version']}"})
            if o["name"] != r["name"]:
                events.append({"date": b, "kind": kind, "app": who,
                               "text": f"[{r['country']}] isim değişti: “{o['name']}” → “{r['name']}”"})
        for s in series:
            ra, rb = s["ranks"][idx[a]], s["ranks"][idx[b]]
            if ra == -1 or rb == -1:
                continue
            ina, inb = ra is not None and ra <= 10, rb is not None and rb <= 10
            label = f"[{s['country']}] “{s['term']}”"
            if inb and not ina:
                events.append({"date": b, "kind": "up", "app": s["app"], "text": f"{label} ilk 10'a girdi ({ra or '200+'} → {rb})"})
            elif ina and not inb:
                events.append({"date": b, "kind": "down", "app": s["app"], "text": f"{label} ilk 10'dan çıktı ({ra} → {rb or '200+'})"})
            elif ra and rb and abs(ra - rb) >= 15:
                events.append({"date": b, "kind": "up" if rb < ra else "down", "app": s["app"], "text": f"{label} {ra} → {rb}"})
            elif (ra is None) != (rb is None):
                events.append({"date": b, "kind": "up" if rb else "down", "app": s["app"],
                               "text": f"{label} {'ilk 200’e girdi: ' + str(rb) if rb else 'ilk 200’den çıktı (' + str(ra) + ')'}"})
    events.sort(key=lambda e: e["date"], reverse=True)

    # Kendi uygulamalarımızın puan sayısı (ülke bazında, son gün)
    ratings = {f"{r['app_id']}|{r['country']}": {"rating": r["rating"], "count": r["rating_count"], "version": r["version"]}
               for r in q("SELECT * FROM app_snapshots WHERE date=? AND app_id IN (SELECT app_id FROM apps WHERE is_mine=1)", last)}
    current_meta = {}
    for r in q("SELECT * FROM metadata WHERE date=(SELECT MAX(date) FROM metadata) AND state=?", LIVE):
        if r["locale"] in locales:
            current_meta[f"{r['app_id']}|{r['locale']}"] = {"name": r["name"], "subtitle": r["subtitle"], "keywords": r["keywords"]}
    # Henüz yayınlanmamış sürümdeki (hazırlanan / incelemede) metadata — uygulama görünümünde fark olarak gösterilir
    pending_meta = {}
    for r in q("SELECT * FROM metadata WHERE date=(SELECT MAX(date) FROM metadata) AND state<>?", LIVE):
        if r["locale"] in locales:
            pending_meta[f"{r['app_id']}|{r['locale']}"] = {"name": r["name"], "subtitle": r["subtitle"],
                                                             "keywords": r["keywords"], "state": r["state"]}
    runs =[dict(r) for r in q("SELECT * FROM runs ORDER BY date DESC LIMIT 14")]

    SITE_DATA.mkdir(parents=True, exist_ok=True)
    out = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "dates": dates,
           "apps": {str(k): v for k, v in mine.items()}, "series": series,
           "top": {str(k): v for k, v in top.items()}, "events": events[:400], "ratings": ratings,
           "meta": current_meta, "pending_meta": pending_meta, "country_locales": {c: COUNTRY_LOCALES.get(c, []) for c in tracked_countries},
           "runs": runs, "popularity": {str(k): v for k, v in pop_hist.items()}}
    (SITE_DATA / "data.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    con.close()
    shutil.copy(DB, ROOT / "site" / "rankings.db")
    print(f"{len(dates)} gün, {len(series)} seri, {len(events)} olay → site/data/data.json")


if __name__ == "__main__":
    export_dashboard(build_db())
