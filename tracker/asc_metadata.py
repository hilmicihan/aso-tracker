"""App Store Connect API'den tüm uygulamaların her dildeki isim / alt başlık / keyword bilgisini çeker.

Ortam değişkenleri:
  ASC_ISSUER_ID   - App Store Connect > Users and Access > Integrations sayfasındaki Issuer ID
  ASC_KEY_ID      - API anahtarının Key ID'si
  ASC_KEY_PATH    - .p8 dosyasının yolu  (ya da ASC_PRIVATE_KEY: dosyanın içeriği, CI için)
"""
import json
import os
import sys
import time
from pathlib import Path

import jwt
import requests

API = "https://api.appstoreconnect.apple.com/v1"


def make_token() -> str:
    key = os.environ.get("ASC_PRIVATE_KEY") or Path(os.environ["ASC_KEY_PATH"]).read_text()
    now = int(time.time())
    return jwt.encode(
        {"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 15 * 60, "aud": "appstoreconnect-v1"},
        key,
        algorithm="ES256",
        headers={"kid": os.environ["ASC_KEY_ID"], "typ": "JWT"},
    )


class ASC:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {make_token()}"

    def get_all(self, url, params=None):
        out = []
        while url:
            r = self.s.get(url if url.startswith("http") else API + url, params=params, timeout=30)
            r.raise_for_status()
            j = r.json()
            out.extend(j["data"])
            url, params = j.get("links", {}).get("next"), None
        return out


def fetch_metadata() -> list[dict]:
    asc = ASC()
    apps = []
    for app in asc.get_all("/apps", {"fields[apps]": "name,bundleId,primaryLocale", "limit": 200}):
        a = app["attributes"]
        app_id = app["id"]

        # İsim + alt başlık (appInfoLocalizations). Birden fazla appInfo olabilir: yayındaki + hazırlanan.
        infos = asc.get_all(f"/apps/{app_id}/appInfos", {"fields[appInfos]": "appStoreState,state", "limit": 50})
        info_locs = {}
        for info in infos:
            state = info["attributes"].get("state") or info["attributes"].get("appStoreState")
            for loc in asc.get_all(f"/appInfos/{info['id']}/appInfoLocalizations",
                                   {"fields[appInfoLocalizations]": "locale,name,subtitle", "limit": 200}):
                la = loc["attributes"]
                info_locs.setdefault(la["locale"], {})[state] = {"name": la.get("name"), "subtitle": la.get("subtitle")}

        # Keyword'ler (appStoreVersionLocalizations), iOS sürümleri
        versions = asc.get_all(f"/apps/{app_id}/appStoreVersions",
                               {"filter[platform]": "IOS", "fields[appStoreVersions]": "versionString,appStoreState,appVersionState",
                                "limit": 5})
        ver_locs = {}
        for v in versions[:3]:
            va = v["attributes"]
            state = va.get("appVersionState") or va.get("appStoreState")
            for loc in asc.get_all(f"/appStoreVersions/{v['id']}/appStoreVersionLocalizations",
                                   {"fields[appStoreVersionLocalizations]": "locale,keywords", "limit": 200}):
                la = loc["attributes"]
                ver_locs.setdefault(la["locale"], {})[f"{va['versionString']} ({state})"] = la.get("keywords")

        apps.append({
            "app_id": app_id,
            "name": a["name"],
            "bundle_id": a["bundleId"],
            "primary_locale": a.get("primaryLocale"),
            "info_localizations": info_locs,
            "keywords_by_locale": ver_locs,
        })
    return apps


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "metadata.json")
    data = fetch_metadata()
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for app in data:
        print(f"{app['app_id']}  {app['name']}  ({app['bundle_id']})  "
              f"diller: {len(app['info_localizations'])}  keyword dilleri: {len(app['keywords_by_locale'])}")
