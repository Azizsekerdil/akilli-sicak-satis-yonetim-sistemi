# -*- coding: utf-8 -*-
"""
ekran_yakala.py — Tanıtım sunumu için gerçek arayüz ekran görüntülerini alır.

Neden bu betik var
------------------
Tanıtım sunumunun ilk sürümünde programın tek bir ekranı yoktu; yalnızca metin
ve rakam vardı. Bir saha satış yazılımını "ekranı olmadan" anlatmak, alıcının
en çok merak ettiği şeyi (günlük kullanımda neye benziyor) cevapsız bırakır.

Bu betik çalışan uygulamayı gerçekten açar, giriş yapar, her ekrana gider ve
görüntüsünü alır. Sunum bu görüntülerle üretilir — çizim veya mockup değil,
programın kendisi.

Ön koşullar:

1.  Arayüz derlenmiş olmalı (``cd frontend && npm ci && npm run build``);
    backend ``frontend/dist`` klasörünü servis eder.
2.  Backend ayakta olmalı::

        cd backend
        python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

3.  Demo verisi yüklenmiş olmalı (``python -m scripts.seed_demo_data --reset``).
4.  **Yakalamada kullanılan yönetici hesabı ilk giriş parola değişimini
    tamamlamış olmalıdır.** Parolası değişmemiş bir hesap, tasarım gereği
    panel dâhil hiçbir ekranı açamaz (bkz. ``app/core/deps.py``); böyle bir
    hesapla yakalama, boş ekranlardan oluşan bir sunum üretir.

Kullanım:
    py -3.11 ekran_yakala.py
    py -3.11 ekran_yakala.py --url http://127.0.0.1:8000 --dil tr
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Türkçe bir Windows konsolu cp1254 kullanır ve "→" gibi karakterlerde
# UnicodeEncodeError ile düşer. Yakalama işi bittikten sonra bir yazdırma
# hatası yüzünden sıfırdan farklı çıkmak, betiği çağıran her otomasyona
# "başarısız" der; çıktıyı UTF-8'e çevirip ASCII yedeğe düşüyoruz.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - çok eski Python / tuhaf konsol
    pass

KOK = Path(__file__).resolve().parent
CIKTI = KOK / "docs" / "presentation" / "ekranlar"

# 16:9 — slayta tam oturur, ölçekleme bulanıklığı olmaz.
GENISLIK, YUKSEKLIK = 1600, 900

#: /ai/providers YAKALANMAZ. Ekran, kayıtlı anahtarı "nvap*******gUqA" gibi
#: maskeleyerek gösterir; maske ön eki ve son dört karakteri açığa çıkarır.
#: Bu, sunuma ve depoya giden bir görüntüde kısmi anahtar sızıntısıdır.
#: Sağlayıcı yapılandırmasını anlatmak gerekirse ekran, anahtar alanı boş bir
#: ortamda yeniden yakalanmalı; bu listeye geri eklenmemeli.

#: Sıcak satış ekranı YÖNETİCİ ile açıldığında "bu kullanıcıya bağlı açık araç
#: yok" uyarısı verir ve katalog moduna düşer — sahadaki hiçbir plasiyerin
#: içinde bulunmayacağı bir durum. O ekran kendi gerçek kullanıcısıyla,
#: müşteri seçilmiş ve sepeti dolu hâlde yakalanır.
PLASIYER = ("demo_plasiyer1", "Demo1234!")


#: (dosya adı, rota, açıklama, hazır olma ölçütü)
#: Ölçüt: sayfanın gerçekten dolduğunu gösteren bir metin/seçici. Sabit süre
#: beklemek yerine içerik beklemek, yavaş makinede boş ekran yakalamayı önler.
EKRANLAR = [
    ("01-giris",          "/login",                    "Giriş ekranı", None),
    ("02-panel",          "/",                         "Kontrol paneli", "text=Kontrol Paneli"),
    ("03-sicak-satis",    "/sales/hot-sale",           "Sıcak satış", "text=Sıcak Satış"),
    ("04-musteriler",     "/crm/customers",            "Müşteriler", "text=Müşteriler"),
    ("05-musteri-detay",  "/crm/customers/1",          "Müşteri detayı", None),
    ("06-urunler",        "/stock/products",           "Ürünler", "text=Ürünler"),
    ("07-depolar",        "/stock/warehouses",         "Depolar", "text=Depolar"),
    ("08-arac-yukleme",   "/stock/van-load",           "Araç yükleme", "text=Araç Yükleme"),
    ("09-arac-stok",      "/stock/vehicle-stock",      "Araç stokları", None),
    ("10-rotalar",        "/field/routes",             "Rotalar", "text=Rotalar"),
    ("11-harita",         "/field/map",                "Harita", None),
    ("12-gun-yonetimi",   "/field/day-sessions",       "Gün yönetimi", None),
    ("13-faturalar",      "/sales/invoices",           "Faturalar", None),
    ("14-tahsilatlar",    "/sales/payments",           "Tahsilatlar", None),
    ("15-kampanyalar",    "/marketing/campaigns",      "Kampanyalar", None),
    ("16-raporlar",       "/analytics/reports",        "Raporlar", None),
    ("17-istatistik",     "/analytics/statistics",     "İstatistik", None),
    ("18-tahminler",      "/analytics/forecasts",      "Tahminler", None),
    ("19-ai-mudur",       "/ai/manager",               "AI Satış Müdürü", None),
    ("21-ai-terminal",    "/ai/terminal",              "AI terminali", None),
    ("22-uyumluluk",      "/compliance",               "Uyumluluk durumu", None),
    ("23-veri-envanteri", "/compliance/inventory",     "Veri envanteri", None),
    ("24-hak-makbuzlari", "/compliance/hsp-receipts",  "Hak makbuzları", None),
    ("25-saglik",         "/system/health",            "Sistem sağlığı", None),
    ("26-denetim",        "/system/audit",             "Denetim kaydı", None),
    ("27-egitim",         "/system/training",          "Eğitim merkezi", None),
    ("28-roller",         "/system/roles",             "Roller ve yetkiler", None),
]


def yakala(temel_url: str, dil: str, kullanici: str, sifre: str) -> list[tuple[str, str]]:
    from playwright.sync_api import sync_playwright

    CIKTI.mkdir(parents=True, exist_ok=True)
    alinan: list[tuple[str, str]] = []

    with sync_playwright() as p:
        tarayici = p.chromium.launch(args=["--force-color-profile=srgb"])
        sayfa = tarayici.new_page(
            viewport={"width": GENISLIK, "height": YUKSEKLIK},
            device_scale_factor=2,          # retina — slaytta keskin durur
            locale="tr-TR" if dil == "tr" else "en-GB",
        )
        sayfa.set_default_timeout(15000)

        # --- giriş ekranı (oturum açmadan) --------------------------------
        sayfa.goto(f"{temel_url}/login", wait_until="networkidle")
        sayfa.wait_for_timeout(700)
        if dil == "en":
            _dili_ayarla(sayfa, "en")
        yol = CIKTI / f"01-giris-{dil}.png"
        sayfa.screenshot(path=str(yol))
        alinan.append(("01-giris", "Giriş ekranı"))
        print(f"  [OK] 01-giris")

        # --- giriş yap -----------------------------------------------------
        try:
            sayfa.fill("#username", kullanici)
            sayfa.fill("#password", sifre)
            sayfa.click("button[type=submit]")
            sayfa.wait_for_url(lambda u: "/login" not in u, timeout=20000)
            sayfa.wait_for_timeout(1500)
        except Exception as exc:
            print(f"  [!] Giriş yapılamadı: {exc}")
            tarayici.close()
            return alinan

        if dil == "en":
            _dili_ayarla(sayfa, "en")
            sayfa.wait_for_timeout(600)

        # --- diğer ekranlar ------------------------------------------------
        for ad, rota, aciklama, olcut in EKRANLAR[1:]:
            try:
                sayfa.goto(f"{temel_url}{rota}", wait_until="networkidle")
                if olcut:
                    try:
                        sayfa.wait_for_selector(olcut, timeout=6000)
                    except Exception:
                        pass
                # Grafikler ve tablolar yerleşsin; recharts animasyonu ~800 ms.
                sayfa.wait_for_timeout(1600)
                sayfa.screenshot(path=str(CIKTI / f"{ad}-{dil}.png"))
                alinan.append((ad, aciklama))
                print(f"  [OK] {ad}")
            except Exception as exc:
                print(f"  [!] {ad} atlandı: {str(exc)[:70]}")

        # Sıcak satış ekranını kendi kullanıcısıyla yeniden yakala.
        _sicak_satis_senaryosu(tarayici, temel_url, dil)

        tarayici.close()
    return alinan


def _sicak_satis_senaryosu(tarayici, temel_url: str, dil: str) -> bool:
    """
    Sıcak satışı gerçek plasiyerle ve dolu sepetle yakalar.

    Boş bir sepetin ekran görüntüsü ürünün ne yaptığını göstermez; bu yüzden
    müşteri seçilir, iki ürün eklenir ve fiyatlandırma dönene kadar beklenir.
    """
    sayfa = tarayici.new_page(
        viewport={"width": GENISLIK, "height": YUKSEKLIK},
        device_scale_factor=2,
        locale="tr-TR" if dil == "tr" else "en-GB",
    )
    sayfa.set_default_timeout(15000)
    try:
        sayfa.goto(f"{temel_url}/login", wait_until="networkidle")
        sayfa.fill("#username", PLASIYER[0])
        sayfa.fill("#password", PLASIYER[1])
        sayfa.click("button[type=submit]")
        sayfa.wait_for_url(lambda u: "/login" not in u, timeout=20000)
        if dil == "en":
            _dili_ayarla(sayfa, "en")
        sayfa.goto(f"{temel_url}/sales/hot-sale", wait_until="networkidle")
        sayfa.wait_for_timeout(2000)

        # Müşteri listesindeki ilk kaydı seç.
        try:
            sayfa.locator("button:has(svg) >> text=/M[0-9]{5}/").first.click(timeout=5000)
        except Exception:
            # Yapı değişmişse: müşteri kartındaki ilk tıklanabilir satır.
            try:
                sayfa.locator("ul li button").first.click(timeout=5000)
            except Exception:
                pass
        sayfa.wait_for_timeout(1800)

        # Araç stoğundan iki ürün ekle.
        eklendi = 0
        for i in range(2):
            try:
                sayfa.locator("button:has-text('SKU')").nth(i).click(timeout=4000)
                eklendi += 1
                sayfa.wait_for_timeout(900)
            except Exception:
                break
        # Kampanya/fiyat sorgusu dönsün.
        sayfa.wait_for_timeout(2200)

        sayfa.screenshot(path=str(CIKTI / f"03-sicak-satis-{dil}.png"))
        print(f"  [OK] 03-sicak-satis (plasiyer, {eklendi} urun sepette)")
        return True
    except Exception as exc:
        print(f"  [!] sicak satis senaryosu: {str(exc)[:80]}")
        return False
    finally:
        sayfa.close()


def _dili_ayarla(sayfa, dil: str) -> None:
    """Arayüzü istenen dile çevirir — dil düğmesi üst çubukta."""
    try:
        sayfa.evaluate(
            "(d) => { localStorage.setItem('vs.lang', d); }", dil
        )
        sayfa.reload(wait_until="networkidle")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Tanıtım için arayüz ekranlarını yakala")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--dil", choices=["tr", "en", "hepsi"], default="hepsi")
    ap.add_argument("--kullanici", default="admin")
    ap.add_argument("--sifre", default=os.getenv("VS_ADMIN_PASSWORD", "AdminTest123!"))
    a = ap.parse_args()

    import urllib.request
    try:
        with urllib.request.urlopen(f"{a.url}/health", timeout=5) as r:
            if r.status != 200:
                raise RuntimeError(str(r.status))
    except Exception as exc:
        print(f"HATA: {a.url} adresinde sunucu yok ({exc})")
        print("Önce backend'i başlatın:")
        print("  cd backend && python -m uvicorn app.main:app --port 8000")
        return 1

    diller = ("tr", "en") if a.dil == "hepsi" else (a.dil,)
    toplam = 0
    for dil in diller:
        print(f"\n=== {dil.upper()} ekranları yakalanıyor ===")
        t0 = time.time()
        alinan = yakala(a.url, dil, a.kullanici, a.sifre)
        toplam += len(alinan)
        print(f"  {len(alinan)} ekran, {time.time() - t0:.0f} sn")

    print("")
    print(f"Toplam {toplam} goruntu -> {CIKTI}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
