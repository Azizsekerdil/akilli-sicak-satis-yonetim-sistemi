# Changelog

Bu projenin tüm önemli değişiklikleri bu dosyada belgelenir.
All notable changes to this project are documented here.

Biçim [Keep a Changelog](https://keepachangelog.com/) esasına,
sürümleme [Semantic Versioning](https://semver.org/) kurallarına dayanır.

---

## [Unreleased] — public release preparation

Bu sürüm kod tabanını kamuya açık yayına hazırlar. Değişikliklerin çoğu güvenlik,
lisans ve doğruluk düzeltmeleridir; hiçbiri yeni bir iş özelliği değildir.

This release prepares the codebase for public distribution. Most changes are
security, licence and truthfulness fixes; none of them is a new business feature.

### Güvenlik / Security

- **İlk kurulum parolası artık sunucu tarafında zorlanıyor.** `must_change_password`
  bayrağı önceden yalnızca istemciye bildiriliyordu; API'yi doğrudan çağıran biri
  ilk parolayla her yere erişebiliyordu. Parola değiştirilene kadar parola değişim
  akışı dışındaki **her** uç nokta 403 döner.
  *The forced password change is now enforced by the API, not merely reported to
  the client. Until the initial password is changed, every endpoint outside the
  password-change flow answers 403.*
- **İlk kurulum hesabı yalnızca yerel cihazdan giriş kabul eder.** Karar soket
  eşinin adresine bakar, `X-Forwarded-For` başlığına değil — sahte başlık kapıyı
  açmaz. Parola değiştiğinde kısıt kalkar ve bir daha kurulmaz; yönetici
  sıfırlaması ilk kurulum durumunu geri getirmez.
  *The first-run credential is refused from anything but the local device, decided
  from the socket peer rather than a forwarded header. Cleared permanently by the
  first password change; an administrative reset does not restore it.*
- **Uyumluluk uç noktaları kendi izinlerine bağlandı.** Hepsi tek bir
  `system.settings` iznine bağlıydı; bu, katalogdaki bütün `compliance.*` ve
  `hsp.*` izinlerini dekoratif hâle getiriyor (Denetçi rolü menüyü görüyor, API'den
  403 alıyordu) ve aynı anda bir AI ayarını değiştirebilen herkese mevzuat paketi
  onaylama yetkisi veriyordu.
  *Each compliance endpoint now enforces its own declared permission. Previously
  they all hung off one setting permission, which made the entire compliance
  permission tree decorative and simultaneously over-privileged.*
- **API anahtarı maskesi artık yalnızca son dört karakteri gösteriyor.** Önceki
  maske ilk dört karakteri de bırakıyordu; bu, sağlayıcıyı ve anahtar sınıfını her
  ekran görüntüsüne sızdırıyordu. Maske genişliği sabit olduğu için anahtar uzunluğu
  da açığa çıkmaz.
  *Secret masking keeps only the last four characters. The previous mask also kept
  the first four, leaking the vendor prefix into every screenshot. The mask width is
  now constant so the length is not disclosed either.*
- Keşif tarayıcısındaki `npm` çağrısından `shell=True` kaldırıldı; yürütülebilir
  açıkça çözülüyor.
  *`shell=True` removed from the dependency scanner's `npm` invocation.*

### Düzeltildi / Fixed

- **Uyumluluk ekranlarının çoğu var olmayan uç noktalara istek atıyordu.** Envanter,
  başvuru, mevzuat paketi, aydınlatma metni ve hak makbuzu ekranları 404 alıyordu.
  Yollar düzeltildi ve eksik uç noktalar gerçekten uygulandı: envanter özeti,
  aydınlatma metni ayrıntısı, mevzuat paketi ayrıntısı, makbuz ayrıntısı, karara
  itiraz ve başvuru durum geçişi.
  *Five of the six compliance screens called endpoints that did not exist. Paths
  corrected and the missing endpoints genuinely implemented.*
- Arayüzdeki üç izin kontrolü var olmayan kaynak adları kullanıyordu
  (`compliance.consents`, `compliance.rule_packs`, `compliance.hsp`) ve bu yüzden
  koşulsuz `false` dönüyor, korudukları düğmeyi sessizce gizliyordu.
  *Three UI permission checks named resources that do not exist and therefore
  silently hid the controls they guarded.*
- `/compliance/hsp/receipts/verify` yolu, parametreli makbuz yolunun arkasında
  kalınca 422 dönüyordu; sabit segmentli rotalar öne alındı.
- 17 kaynak dosyasındaki UTF-8 BOM temizlendi (statik çözümleyicileri bozuyordu).
- Ekran yakalama betiği, Türkçe Windows konsolunda son satırı yazarken
  `UnicodeEncodeError` ile düşüyordu.

### Değişti / Changed

- **`react-leaflet` kaldırıldı** (Hippocratic-2.1; OSI onaylı değil, kullanım
  kısıtı içerir). Harita ekranı doğrudan **Leaflet** (BSD-2-Clause) API'siyle
  yeniden yazıldı; davranış korunmuştur.
  *`react-leaflet` removed for licence reasons; the map screen was rewritten
  against plain Leaflet (BSD-2-Clause) with behaviour preserved.*
- **Bağımlılıklar güvenlik danışmalarının üstüne yükseltildi**: FastAPI 0.141.1,
  Starlette 1.6.0, PyJWT 2.13.0, python-multipart 0.0.32, python-dotenv 1.2.3,
  SQLAlchemy 2.0.52, pydantic 2.13.4, uvicorn 0.52.4, bcrypt 5.0.0, pytest 9.0.3.
  Yayın tarihinde `pip-audit`, `npm audit`, `grype` ve `trivy` temiz.
- **Demo veri iletişim bilgileri üretim anında maskeleniyor.** Telefonlar
  `+90 5XX XXX XX 42` biçiminde ve aranabilir değil; e-postalar RFC 2606'nın
  ayırdığı `demo.invalid` alan adını kullanıyor; vergi numaraları sabit `0000`
  öneki taşıyor. Ekran görüntüsünü sonradan maskelemek unutulabilir bir adımdır.
- **Sunumdaki her rakam artık koddan ölçülüyor** (`tanitim_uret.py::_olcum`).
  Önceki slaytlar elle yazılmış ve uyumluluk katmanı eklendiğinde geçersizleşmiş
  sayılar taşıyordu (71 tablo / 259 uç nokta / 170 izin yerine gerçek değerler).
- Sunum çıktıları `docs/presentation/` altına ve `_PUBLIC` ekiyle üretiliyor;
  özgün dosyanın üzerine yazılmıyor.
- README yeniden yazıldı: ürünün yapmadıkları, olgunluk düzeyi, iddia sınırları ve
  ölçülmüş sayılar açıkça yer alıyor.

### Eklendi / Added

- `GET /compliance/inventory/summary` — envanter ekranının üst şeridi
- `GET /compliance/notices/{id}` — aydınlatma metni ayrıntısı (gövde ile)
- `GET /compliance/rulepacks/{id}` — mevzuat paketi ayrıntısı
- `GET /compliance/hsp/receipts/{id}` — makbuz ayrıntısı, varsa itiraz durumu
- `POST /compliance/hsp/receipts/{id}/appeal` — karara itiraz; makbuz
  değiştirilmez, itiraz `AUTOMATED_DECISION_REVIEW` türünde bir ilgili kişi
  başvurusu olarak kaydedilir
- `POST /compliance/dsr/{id}/transition` — açık başvurunun durum değişikliği;
  kapanış kasıtlı olarak hariç, o `/fulfil` üzerinden yürür
- Zorunlu parola değişimi ekranı (`ForcePasswordChange.tsx`)
- Alembic göçü `b1f4c7d92a08` — `users.is_bootstrap_credential`
- **150 yeni test** (247 → 397): ilk kurulum kimlik bilgisi sözleşmesi, uyumluluk
  RBAC bağlamaları, arayüz ↔ API sözleşmesi, API anahtarı politikası, demo veri
  gizliliği, uyumluluk ekranlarının uçtan uca doğrulaması
- Kamuya açık yayın belgeleri: `SECURITY.md`, `PRIVACY.md`, `AI_TRANSPARENCY.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `THIRD_PARTY_NOTICES.md`,
  `LICENSE_DECISION_PENDING.md`, `docs/known-limitations.md`
- SBOM: `sbom.spdx.json` (SPDX 2.3), `sbom.cdx.json` (CycloneDX 1.7)
- GitHub Actions CI ve Dependabot yapılandırması

### Kaldırıldı / Removed

- İç kullanım belgeleri (uygulama planları, keşif raporu, HSP proje incelemesi,
  patent çalışma alanı açıklaması) kamuya açık dağıtımdan çıkarıldı. Tamamlanmış
  düzeltmeler bu changelog girdisinde özetlenmiştir.
  *Internal working documents were excluded from the public distribution; the
  completed fixes are summarised in this entry instead.*

---

## [1.0.0] — 2026-08-18

İlk sürüm. Sıcak satış operasyonunun tamamını kapsayan çekirdek sistem.
First release. Core system covering the complete van sales operation.

### Eklendi / Added

**Altyapı / Foundation**
- Python 3.11 + FastAPI + SQLAlchemy 2.0 (senkron) + Pydantic v2 katmanlı mimari
- SQLite varsayılan, PostgreSQL yapılandırmayla desteklenir; taşınabilir SQL
- Alembic göç sistemi (yukarı/aşağı doğrulandı). *Bu girdideki tablo/sütun/indeks
  sayıları yayına hazırlık sırasında yanlış bulundu ve çıkarıldı: uyumluluk
  katmanı 1.0.0 içinde yer aldığı hâlde sayılar onu içermiyordu. Güncel ve
  ölçülmüş değerler README'dedir.*
- Değiştirilemez `stock_movements` defteri + malzemelenmiş `stock_balances`
- `UTCDateTime` tipi — SQLite ve PostgreSQL arasında saat dilimi davranışını eşitler
- `Money` / `Quantity` Decimal sütun tipleri; para hesabı hiçbir yerde float değil
- Çevrimdışı senkronizasyon için `client_uid` idempotens anahtarları
- Türkçe-güvenli arama için ASCII'ye katlanmış `search_key` sütunları

**Kimlik ve yetki / Identity & access**
- 19 rol; ekran + işlem + veri kapsamı olmak üzere 3 katman. *Kaynak ve izin
  sayıları yukarıdaki nedenle çıkarıldı; ölçülmüş değerler README'dedir.*
- JWT erişim jetonu + döndürülen yenileme jetonu, sunucuda iptal edilebilir oturum
- Yetki yükseltme koruması (üst rol atanamaz, sahip olunmayan izin verilemez)
- Hesap kilitleme, IP başına kayan pencere hız sınırı, güvenlik başlıkları
- Zincirlenmiş SHA-256 denetim kaydı + bütünlük doğrulama uç noktası

**İş modülleri / Business modules**
- Ürün, kategori, marka, birim dönüşümü, barkod, fiyat listesi
- Depo yönetimi, FEFO/FIFO tahsis, lot izlenebilirliği, transfer, sayım
- Araç = mobil depo; yükleme, boşaltma, kapasite kontrolü
- Gün oturumu ve gün sonu mutabakatı (teorik/fiziki fark analizi)
- Müşteri/CRM, cari hesap defteri, yaşlandırma, risk skoru, churn tespiti
- Sıcak satış tek işlem akışı: sipariş → satış → fatura → tahsilat → stok
- İade (satır bazında sebep ve tasarruf kararı), iade faturası
- Kampanya motoru: 8 kampanya tipi, koşul/ödül modeli, kârlılık ölçümü

**Rota ve saha / Routing & field**
- Clarke-Wright tasarruf + 2-opt/Or-opt yerleşik VRP çözücüsü (bağımlılıksız)
- OR-Tools opsiyonel entegrasyonu; kurulu değilse yedek çözücü devreye girer
- Kapasite, zaman penceresi, servis süresi, öncelikli müşteri kısıtları
- GPS izleme, geofence doğrulama, planlanan/gerçekleşen sapma analizi

**Analitik / Analytics**
- 20 KPI'lık kontrol paneli, 7 canlı grafik
- Tanımlayıcı istatistik, zaman serisi ayrıştırma, korelasyon, regresyon
- Kesintili talep tahmini: Croston, SBA, TSB, Holt-Winters, mevsimsel naif;
  yöntem geriye dönük testle seçilir
- ABC analizi, sepet analizi, anomali tespiti
- 21 rapor tanımı; PDF / Excel / CSV dışa aktarım

**Yapay zeka / AI**
- Sağlayıcı soyutlaması: LM Studio, NVIDIA NIM, Anthropic Claude
- Görev tipine göre model yönlendirme + yedekleme zinciri + sağlık takibi
- Token ve maliyet muhasebesi, aylık bütçe sınırı (yerel model muaf)
- 8 uzman ajan (satış, tahmin, rota, stok, tahsilat riski, raporlama, analist)
- Salt okunur NL→SQL kapısı: tek SELECT, hassas tablolar engelli, satır limiti
- Kademeli yetkili AI terminali; yıkıcı işlemler hiçbir seviyede yürütülmez

**Arayüz / Frontend**
- React 18 + TypeScript + Vite + Tailwind; kurumsal SaaS görünümü
- PWA (service worker, offline kabuk, kurulabilir uygulama)
- Ctrl+K komut paleti, bildirim merkezi, TR/EN anlık dil değişimi
- İzne göre daralan kenar menü

**İşletim / Operations**
- `setup.ps1` ile tek komutla kurulum, `start.bat` ile başlatma
- SQLite çevrimiçi yedekleme API'si ile tutarlı yedek + SHA-256 doğrulama
- Geri yükleme öncesi doğrulama ve otomatik güvenlik yedeği
- 9 bileşenli sistem sağlık ekranı
- 14 derslik interaktif eğitim merkezi
- Gerçekçi sentetik demo veri üreteci (12 aylık geçmiş, mevsimsellik dâhil)

### Güvenlik / Security
- Sırlar `.env` içinde; `.gitignore` ile korunur
- Loglara yazılmadan önce kimlik bilgisi redaksiyonu (JWT, API anahtarı, parola)
- API yanıtlarında anahtar maskeleme; veritabanında anahtar saklanmaz
- Bağımlılık taraması: 74 pip + 510 npm paketinde GPL/AGPL/LGPL yok

### Bilinen sınırlar / Known limitations
- SQLite'ta SQL tarafı `SUM()` float aritmetiği kullanır; sonuçlar Python'da
  Decimal olarak yeniden yuvarlanır (hata payı ~10⁻⁹ TL, kuruşun çok altında).
  Tam ondalık toplama gerekiyorsa PostgreSQL kullanın.
- `react-leaflet` Hippocratic-2.1 lisanslıdır (OSI onaylı değil). **Bu sınır
  Unreleased sürümünde giderildi**: paket kaldırıldı, harita ekranı doğrudan
  Leaflet (BSD-2-Clause) ile yeniden yazıldı. Ayrıntı:
  `THIRD_PARTY_NOTICES.md`.
- Zamanlanmış işler süreç-içi çalışır; çok sunuculu dağıtımda harici bir
  zamanlayıcı gerekir.
