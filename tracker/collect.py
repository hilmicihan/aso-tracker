"""Günlük toplama: sıralamalar (iTunes Search API) + kendi uygulamalarımızın metadata'sı (App Store Connect API).

Çıktı: data/raw/<YYYY-MM-DD>/ altında
  rankings.csv.gz   country, term, rank, app_id        (ilk TOP_N + bizim uygulamalar ilk 200'de nerede olursa)
  apps.csv.gz       country, app_id, name, developer, version, version_date, rating, rating_count, price, genre
  metadata.json.gz  App Store Connect'teki isim/alt başlık/keyword'ler (ASC_* ortam değişkenleri varsa)
  keywords.csv      o günkü hedef listesi (keywords.csv'nin kopyası)
  run.json          çalışma özeti

Kullanım:  python -m tracker.collect [--date YYYY-MM-DD]
"""
import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
TOP_N = 50          # her aramada saklanan ilk N sonuç (rakip takibi için)
SNAPSHOT_TOP = 10   # ilk kaç sonucun uygulama detayı saklansın
DELAY = 3.2         # istekler arası bekleme (Apple ~20 istek/dk sınırı)

session = requests.Session()
session.headers["User-Agent"] = "aso-rank-tracker/1.0"


def get_json(url, params, tries=4):
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code in (403, 429, 503):
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            wait = 20 * (i + 1)
            print(f"   ! {e} — {wait}s sonra tekrar", flush=True)
            time.sleep(wait)


def app_row(country, x):
    return {
        "country": country,
        "app_id": x["trackId"],
        "name": x.get("trackName", ""),
        "developer": x.get("artistName", ""),
        "version": x.get("version", ""),
        "version_date": x.get("currentVersionReleaseDate", ""),
        "rating": round(x.get("averageUserRating") or 0, 3),
        "rating_count": x.get("userRatingCount") or 0,
        "price": x.get("price") or 0,
        "genre": x.get("primaryGenreName", ""),
    }


def write_csv_gz(path, rows, fields):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())


def collect_metadata(out_dir, summary):
    if not (os.environ.get("ASC_ISSUER_ID") and os.environ.get("ASC_KEY_ID")
            and (os.environ.get("ASC_PRIVATE_KEY") or os.environ.get("ASC_KEY_PATH"))):
        print("ASC bilgileri yok, metadata atlanıyor.")
        summary["metadata"] = "skipped"
        return
    from tracker.asc_metadata import fetch_metadata
    try:
        meta = fetch_metadata()
        with gzip.open(out_dir / "metadata.json.gz", "wt", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        summary["metadata"] = f"ok ({len(meta)} apps)"
        print(f"Metadata: {len(meta)} uygulama")
    except Exception as e:  # noqa: BLE001
        summary["metadata"] = f"error: {e}"
        summary["errors"].append(f"metadata: {e}")
        print(f"Metadata hatası: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    args = ap.parse_args()

    kw_rows = list(csv.DictReader(open(ROOT / "keywords.csv", encoding="utf-8")))
    my_ids = {int(r["app_id"]) for r in kw_rows}
    queries = sorted({(r["country"].strip().lower(), r["term"].strip()) for r in kw_rows})

    out_dir = RAW / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "keywords.csv", out_dir / "keywords.csv")
    started = time.time()
    summary = {"date": args.date, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "queries": len(queries), "errors": []}

    collect_metadata(out_dir, summary)

    rankings, apps = [], {}
    failed = 0
    for i, (country, term) in enumerate(queries, 1):
        try:
            res = get_json(SEARCH_URL, {"term": term, "country": country, "entity": "software", "limit": 200})["results"]
        except Exception as e:  # noqa: BLE001
            failed += 1
            summary["errors"].append(f"{country}/{term}: {e}")
            print(f"{i}/{len(queries)} {country} {term!r}: HATA {e}", flush=True)
            continue
        mine_here = []
        for rank, x in enumerate(res, 1):
            aid = x["trackId"]
            if rank <= TOP_N or aid in my_ids:
                rankings.append({"country": country, "term": term, "rank": rank, "app_id": aid})
            if rank <= SNAPSHOT_TOP or aid in my_ids:
                apps[(country, aid)] = app_row(country, x)
            if aid in my_ids:
                mine_here.append(f"{x.get('trackName', aid)[:20]}#{rank}")
        print(f"{i}/{len(queries)} {country} {term!r}: {len(res)} sonuç {' '.join(mine_here)}", flush=True)
        time.sleep(DELAY)

    # Kendi uygulamalarımızın puan/sürüm bilgisi, aramalarda çıkmasalar bile
    for country in sorted({c for c, _ in queries}):
        try:
            res = get_json(LOOKUP_URL, {"id": ",".join(map(str, sorted(my_ids))), "country": country})["results"]
            for x in res:
                apps[(country, x["trackId"])] = app_row(country, x)
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"lookup {country}: {e}")
        time.sleep(DELAY)

    write_csv_gz(out_dir / "rankings.csv.gz", rankings, ["country", "term", "rank", "app_id"])
    write_csv_gz(out_dir / "apps.csv.gz", list(apps.values()),
                 ["country", "app_id", "name", "developer", "version", "version_date",
                  "rating", "rating_count", "price", "genre"])

    summary.update({"finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "duration_s": round(time.time() - started), "failed_queries": failed,
                    "ranking_rows": len(rankings), "app_rows": len(apps),
                    "status": "ok" if failed == 0 else ("failed" if failed == len(queries) else "partial")})
    (out_dir / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Bitti: {summary['status']}, {len(rankings)} sıralama satırı, {summary['duration_s']} sn")
    if summary["status"] == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
