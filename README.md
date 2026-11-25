# ASO Rank Tracker

App Store uygulamalarının keyword sıralamalarını her gün otomatik ölçer, geçmişi saklar ve bir dashboard'da gösterir.

- **Sıralamalar:** iTunes Search API (ücretsiz, kişiselleştirilmemiş). Her ifade için ilk 200 sonuca bakılır; ilk 50 sonuç (rakipler) ve bizim uygulamaların sırası saklanır.
- **Metadata:** App Store Connect API ile her dildeki isim / alt başlık / keyword'ler günlük kaydedilir; değişiklikler olay akışında görünür.
- **Çalışma:** GitHub Actions her gün 05:15 UTC'de (08:15 TR) çalışır, ham veriyi repoya commit'ler, dashboard'u GitHub Pages'e yayınlar.

## Dosyalar

| Yol | Ne |
|---|---|
| `keywords.csv` | Takip listesi: `app_id, app, country, term`. Düzenlemek için tek yer burası. |
| `tracker/collect.py` | Günlük toplama → `data/raw/<tarih>/` |
| `tracker/asc_metadata.py` | App Store Connect API'den metadata |
| `tracker/build.py` | Ham veriden `data/rankings.db` (SQLite) + `site/data/data.json` |
| `site/index.html` | Dashboard |
| `data/raw/<tarih>/` | Asıl kayıt: sıkıştırılmış günlük CSV'ler (günde ~30 KB) |

SQLite dosyası git'e eklenmez; her çalışmada ham veriden yeniden kurulur ve dashboard'dan indirilebilir.

## GitHub kurulumu (bir kerelik)

1. **Secrets** (Settings → Secrets and variables → Actions → New repository secret):
   - `ASC_ISSUER_ID` — App Store Connect → Users and Access → Integrations sayfasındaki Issuer ID
   - `ASC_KEY_ID` — API anahtarının Key ID'si
   - `ASC_PRIVATE_KEY` — `.p8` dosyasının **tüm içeriği** (`-----BEGIN PRIVATE KEY-----` satırları dahil)
2. **Pages:** Settings → Pages → Source: **GitHub Actions**
3. **İlk çalıştırma:** Actions → *Daily ASO scan* → **Run workflow**

## Yerelde çalıştırma

```powershell
pip install -r requirements.txt
$env:ASC_ISSUER_ID="..."; $env:ASC_KEY_ID="..."; $env:ASC_KEY_PATH="C:\yol\AuthKey_XXXX.p8"
python -m tracker.collect
python -m tracker.build
python -m http.server -d site 8000   # http://localhost:8000
```

ASC değişkenleri verilmezse metadata atlanır, sıralama takibi yine çalışır.

## Popülerlik (Mobile Action)

`data/popularity/<YYYY-MM-DD>.csv` dosyaları elle/yarı otomatik eklenir ve dashboard'daki **Pop.** sütununu besler.
Sütunlar: `country, term, popularity, difficulty, ranked_apps, source`. Değer yoksa (Mobile Action'da N/A) boş bırakılır.
Popülerlik Apple Search Ads ölçeğindedir: 5 en düşük, 100 en yüksek. Her ifade için en son ölçüm gösterilir; eski dosyalar geçmiş olarak kalır.

Güncellemek için Mobile Action'da oturum açıkken Claude'a "Mobile Action popülerliğini güncelle" demek yeterli:
Keyword Inspector'dan `keywords.csv`'deki tüm ifadeleri okuyup yeni tarihli bir dosya olarak ekler.

## Notlar

- iTunes Search API sırası App Store uygulamasındaki sırayla birebir aynı değildir (özellikle alt sıralarda). Trend takibi için güvenilirdir.
- Apple ~20 istek/dakika sınırı koyar; istekler arası 3,2 sn beklenir. 100 ifade ≈ 6 dakika.
- Public repolarda 60 gün commit olmazsa zamanlanmış Actions durur; günlük veri commit'i bunu engeller.
