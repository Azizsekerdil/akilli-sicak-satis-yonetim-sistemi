# Mimari / Architecture

**Akıllı Sıcak Satış Yönetim Sistemi — Smart Van Sales Management System**

> Bu belge sistemin mimari kararlarını ve gerekçelerini açıklar.
> This document records the architectural decisions and the reasoning behind them.

---

## 1. Hedef ortam / Target environment

Doğrulanmış kurulum (2026-08-15, bu makinede ölçüldü):

| Bileşen | Durum | Karar üzerindeki etkisi |
|---|---|---|
| Windows 11 Pro 26200 | ✔ | Birincil hedef; tüm betikler PowerShell/CMD |
| Python 3.11.9 | ✔ | Backend runtime (3.14 de kurulu, 3.11 tercih edildi — wheel uyumu) |
| Node.js 24.15 / npm 11.12 | ✔ | Frontend derleme |
| Git 2.55 + gh 2.96 (authenticated) | ✔ | Sürüm kontrolü ve yayın |
| **PostgreSQL** | ✘ kurulu değil | → **SQLite varsayılan**, PostgreSQL yapılandırmayla desteklenir |
| **Redis** | ✘ kurulu değil | → süreç-içi önbellek/kuyruk, Redis opsiyonel |
| **Docker** | ✘ kurulu değil | → Docker zorunlu değil; compose dosyası opsiyonel |
| **LM Studio** | ✔ `:1234` çalışıyor, 6 model | → Yerel AI birincil sağlayıcı |
| **NVIDIA API** | ✔ anahtar mevcut, 102 model | → Bulut AI ikincil sağlayıcı |

Bu tablo mimarinin çıkış noktasıdır: **kurulu olmayan hiçbir şeyi zorunlu kılmıyoruz.**
Sistem, hiçbir ek servis kurulmadan `setup.ps1` ile çalışır hale gelir.

---

## 2. Katmanlı yapı / Layered structure

```
frontend/          React + TypeScript + Vite  (SPA + PWA)
        │  REST /api/v1  (JWT Bearer)
        ▼
backend/app/
  api/v1/          HTTP katmanı — yetki, doğrulama, serileştirme
  services/        İş kuralları — tek gerçek kaynağı
  models/          SQLAlchemy 2.0 ORM  (tablo sayısı için README'ye bakınız —
                   ölçülür, elle yazılmaz)
  schemas/         Pydantic v2 istek/yanıt sözleşmeleri
  core/            config, db, güvenlik, RBAC, i18n, log, hata, yardımcılar
  ai/              sağlayıcılar, yönlendirici, ajanlar, SQL güvenliği
  analytics/       istatistik, zaman serisi, tahmin, anomali
  routing/         VRP çözücüler (OR-Tools + bağımsız yedek)
  reports/         rapor motoru + PDF/Excel/CSV dışa aktarım
```

**Kural:** API katmanı iş kuralı içermez; servis katmanı HTTP bilmez.
Bu ayrım sayesinde aynı iş mantığı REST, planlı görev ve AI ajanı tarafından
aynı şekilde çağrılabilir.

---

## 3. Veri modeli kararları / Data model decisions

### 3.1 Stok: değiştirilemez defter + malzemelenmiş bakiye

```
stock_movements   (append-only ledger)  ── tek gerçek kaynağı
stock_balances    (materialised)        ── O(1) okuma için türetilmiş
```

`stock_movements` satırları **asla güncellenmez veya silinmez**. Düzeltmeler yeni
hareket olarak yazılır. `stock_balances` aynı işlem (transaction) içinde
güncellenir, dolayısıyla ikisi hiçbir zaman ayrışamaz; ayrışsa bile bakiye
defterden yeniden üretilebilir.

Bu, ERPNext / OpenBoxes / Apache OFBiz'in yakınsadığı desendir ve araç
mutabakatının *kanıtlanabilir* olmasını sağlayan şeydir: gün sonu farkı
tartışıldığında hareket dökümü hakemdir.

**Bakiye anahtarı:** `(warehouse_id, product_id, lot_id, status)`
**`balance_after` grenliği:** `(warehouse_id, product_id)` — tüm lotlar toplamı.
Bu bilinçli bir seçimdir: stok kartı ekranı ürün bazında yürüyen bakiye ister.
Lot bazlı bakiye her zaman `stock_balances`'tan okunur.
Sıralama daima `(moved_at, id)` iledir; `id` monoton olduğu için aynı saniyedeki
hareketlerde sıra belirsizliği oluşmaz.

### 3.2 Araç = depo

Satış aracı `warehouse_type = VEHICLE` olan bir depodur ve `Vehicle.warehouse_id`
ile birebir bağlıdır. Böylece **tüm** stok kuralları (FEFO, rezervasyon, sayım,
transfer, değerleme) araçta da değişmeden geçerlidir. Ayrı bir "araç stoğu"
alt sistemi yazmak, aynı kuralların ikinci ve zamanla ayrışan bir kopyasını
üretmek olurdu.

### 3.3 FEFO — gıda/içecek için varsayılan

Tahsis stratejisi sırası: fonksiyon argümanı → `Warehouse.allocation_strategy`
→ `settings.stock_allocation_strategy` (varsayılan **FEFO**).

FEFO sıralaması: `Lot.expiry_date` artan (NULL en sona), sonra `lot_id`.
Bloke (`is_blocked`) ve —açıkça izin verilmedikçe— SKT'si geçmiş lotlar atlanır.
Lot takibi kapalı ürünler `lot_id = 0` sentinel bakiyesi üzerinden yürür.

Yiyecek-içecek dağıtımında en yakın SKT'li malın önce çıkması bir tercih değil,
zorunluluktur; bu yüzden varsayılan FEFO'dur, FIFO opsiyoneldir.

### 3.4 Belge zinciri

```
Order  ──►  Sale  ──►  Invoice  ──►  Payment
(talep)    (teslim)   (mali belge)  (tahsilat)
                │
                └──►  Return  ──►  Credit Note
```

- **Order**: müşterinin istediği (ön satış veya sıcak satış)
- **Sale**: fiilen teslim edilen — **stoğu hareket ettiren tek belge budur**
- **Invoice**: fatura / irsaliye / iade faturası
- **Payment**: tahsilat; `PaymentAllocation` ile faturalara dağıtılır

Sıcak satışta üçü tek işlemde oluşur; ön satışta zamana yayılır. Aynı boru
hattı iki senaryoyu da taşır — ikinci bir alt sistem yoktur.

`SaleItem` **lot bazındadır**: bir sipariş satırı iki lota bölünürse iki
`SaleItem` yazılır. Gıda güvenliğinde geri çağırma (recall) izlenebilirliği
bunu gerektirir.

### 3.5 Gün sonu mutabakatı

```
teorik = açılış + yüklenen + ek_yükleme - satılan + iade - fire
fark   = teorik - fiziksel_sayım
```

Her terim `stock_movements` üzerinden hesaplanır — hiçbiri elle tutulan bir
sayaç değildir. Fark sıfırdan farklıysa `DaySession.has_variance` işaretlenir,
`STOCK_VARIANCE` denetim kaydı ve bildirimi üretilir.

### 3.6 Para ve miktar

Para `Numeric(18,4)`, miktar `Numeric(18,3)` olarak saklanır; Python tarafında
**daima `Decimal`** ile hesaplanır (`app.core.utils.money/qty`).

**Bilinen karakteristik:** SQLite'ın yerel `NUMERIC` tipi yoktur; değerler
disk üzerinde IEEE-754 double olarak tutulur. ORM okuma yolunda değer tekrar
4 haneye yuvarlanır (`Money` TypeDecorator), dolayısıyla uygulama kodu her
zaman tam Decimal görür. Ancak **SQL tarafında `SUM()`** float aritmetiği
kullanır: 1.000.000 satırlık bir toplamada birikimli hata ~10⁻⁹ TL
mertebesindedir — kuruş biriminin çok altında, fakat matematiksel olarak tam
değildir. Bu nedenle tüm toplam sonuçları Python'da `money()` ile yeniden
yuvarlanır. PostgreSQL'de `NUMERIC` yerel olduğundan bu durum hiç oluşmaz;
büyük hacimli kurulumlar için PostgreSQL önerilir (§8).

### 3.7 Çevrimdışı güvenliği: `client_uid`

Sahadaki PWA çevrimdışı çalışıp sonradan senkronize olduğu için, gönderilen her
belge istemcide üretilen bir UUID (`client_uid`) taşır ve bu alan benzersizdir.
Aynı satış iki kez gönderildiğinde ikinci istek yeni bir kayıt yaratmaz,
mevcut belgeyi döndürür. Tekrar gönderim, stok ve parayı çift işlemenin en
yaygın nedenidir; bu yüzden idempotenslik veritabanı kısıtı ile garanti altına
alınmıştır.

### 3.8 Türkçe arama

SQLite'ın `LOWER()` fonksiyonu yalnızca ASCII üzerinde çalışır: `İ` → `i`
dönüşümü yapılmaz, dolayısıyla `ŞİŞLİ` araması `şişli` kaydını bulamaz.
Bu nedenle `customers` ve `products` tablolarında yazma anında doldurulan,
ASCII'ye katlanmış bir `search_key` sütunu tutulur ve aramalar bu sütun
üzerinden yapılır (`app.core.utils.slugify` Türkçe harita ile).

---

## 4. Güvenlik ve yetkilendirme / Security & RBAC

Üç katmanlı yetki:

1. **Ekran** — rolün kaynağa erişimi var mı? (`stock.products`)
2. **İşlem** — `VIEW / CREATE / UPDATE / DELETE / APPROVE / EXPORT / EXECUTE`
3. **Veri kapsamı** — `ALL / REGION / TEAM / OWN / NONE`

Roller, kaynaklar ve izinler kodda tek kaynaktan (`app/core/permissions.py`)
tanımlanır; veritabanı bu katalogdan tohumlanır. Kullanıcı bazında
`grant` / `revoke` istisnaları JSON olarak saklanır.

**Yetki yükseltme koruması:** bir kullanıcı kendi `rank` değerinden üstün bir rol
atayamaz ve sahip olmadığı bir izni başkasına veremez.

Diğer önlemler: bcrypt (72-byte üstü parolalar için SHA-256 ön-özet), JWT
erişim + döndürülen (rotating) yenileme jetonu, sunucu tarafında iptal
edilebilir oturumlar, başarısız giriş sayacı ve hesap kilitleme, IP başına
kayan pencere hız sınırı (kimlik uçlarında daha sıkı), güvenlik başlıkları,
Pydantic ile giriş doğrulama, ORM parametreleştirmesi ile SQL enjeksiyon
koruması, ve **loglara yazılmadan önce kimlik bilgilerini temizleyen** bir
redaksiyon filtresi.

**Denetim kaydı zincirlenmiştir:** her satır
`sha256(önceki_checksum + içerik)` taşır. Geçmiş bir satırın değiştirilmesi
zinciri kırar ve `/system/audit/verify` kırılmanın tam yerini bildirir.
API'de bu tabloya güncelleme veya silme yolu yoktur.

---

## 5. Rota optimizasyonu / Route optimisation

İki katmanlı, **her zaman çalışan** tasarım:

| Katman | Teknoloji | Lisans | Durum |
|---|---|---|---|
| Birincil | Google OR-Tools `constraint_solver` | Apache-2.0 | Opsiyonel — kuruluysa kullanılır |
| Yedek | Clarke-Wright tasarruf + 2-opt / Or-opt | Kendi kodumuz | **Her zaman mevcut** |

`app.routing.optimize()` OR-Tools kuruluysa onu, değilse yerleşik sezgisel
çözücüyü kullanır ve `solver_name` ile hangisinin çalıştığını bildirir.
**ImportError hiçbir zaman kullanıcıya yansımaz.** 100 MB'lık bir bağımlılığın
kurulu olmaması bir özelliği tamamen kaybettirmemelidir.

Kısıtlar: araç hacim/ağırlık kapasitesi, müşteri çalışma saatleri (zaman
pencereleri), servis süresi, mesai süresi, öncelikli müşteriler, başlangıç ve
dönüş deposu. Mesafe matrisi haversine × yol sapma katsayısı (varsayılan 1.35)
ile hesaplanır — harici bir yönlendirme sunucusu gerekmez.

Çözücü **determiniktir**: aynı girdi aynı rotayı verir. Saha ekibine "dün
neden farklıydı" sorusunun cevabı verilebilir olmalıdır.

---

## 6. Tahminleme / Forecasting

FMCG talebi **kesintili (intermittent)** ve **düzensiz (lumpy)** olma
eğilimindedir: bir bakkal her gün değil, haftada iki gün sipariş verir. Klasik
ARIMA/Holt-Winters bu seriler üzerinde kötü çalışır. Bu yüzden önce seriyi
sınıflandırıp yöntemi ona göre seçiyoruz:

| Seri tipi | Ölçüt | Yöntem |
|---|---|---|
| Düzgün | ADI < 1.32, CV² < 0.49 | Holt-Winters / hareketli ortalama |
| Kesintili | ADI ≥ 1.32, CV² < 0.49 | **SBA** (Syntetos-Boylan) |
| Düzensiz | ADI ≥ 1.32, CV² ≥ 0.49 | **TSB** (Teunter-Syntetos-Babai) |
| Mevsimsel baskın | haftalık desen güçlü | Haftanın günü mevsimsel naif |

Takvim **sıfırlarla doldurulur** — kesintili talebi tanımlayan şey zaten
sıfırlardır; onları atlamak talebi sistematik olarak abartır.

Tüm yöntemler numpy ile elde yazılmıştır. `prophet`, `statsmodels`,
`scikit-learn` bilinçli olarak **eklenmemiştir**: Windows'ta derleme yükü
getirirler ve bu problem sınıfı için ölçülebilir bir kazanç sağlamazlar.

Her tahmin geriye dönük test edilir (MAE/MAPE/RMSE) ve `Forecast` tablosuna
yöntem + güven aralığı + açıklama ile yazılır. Ölçülemeyen tahmin güvenilmez
tahmindir.

---

## 7. Yapay zeka mimarisi / AI architecture

```
                    ┌──────────────────┐
   Kullanıcı  ────► │  AI Orchestrator │
                    └────────┬─────────┘
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   Sales / Forecast    Inventory / Route    Data Analyst
   Collection Risk     Reporting            (NL → SQL)
        └────────────────────┼────────────────────┘
                             ▼
                    ┌──────────────────┐
                    │    AI Router     │  görev tipine göre model seçer
                    └────────┬─────────┘
             ┌───────────────┼───────────────┐
             ▼               ▼               ▼
        LM Studio        NVIDIA NIM       Claude
        (yerel, ücretsiz)  (bulut)       (bulut)
```

**Yerel öncelikli.** Varsayılan yedekleme sırası `lmstudio → nvidia → claude`.
Yerel model işi görüyorsa para harcanmaz. Aylık bütçe aşıldığında **ücretli**
sağlayıcılar durdurulur, yerel sağlayıcı çalışmaya devam eder.

Görev → model eşlemesi veri tabanında (`AIProviderConfig.task_model_map`):
genel analiz, görsel, matematik, kodlama, uzun bağlam ve gömme (embedding)
görevleri farklı modellere yönlendirilir.

**Anahtar güvenliği:** API anahtarları veritabanında **saklanmaz**; yalnızca
`api_key_ref` (ortam değişkeni adı) ve `has_api_key` bayrağı tutulur. Gerçek
değer `.env` içindedir, `.gitignore` ile korunur, log filtresinden geçer ve
API yanıtlarında `mask_secret()` ile maskelenir.

**NL → SQL güvenliği:** yalnızca tek bir `SELECT`/`WITH` ifadesine izin verilir.
Çoklu ifade, DDL/DML, yorumla kaçırma, ve `users` / `user_sessions` /
`login_attempts` / `audit_logs` / `ai_provider_configs` tablolarına erişim
engellenir; her sorguya satır limiti uygulanır.

**AI Terminal** kademeli izinle çalışır:
`READ_ONLY → PROJECT_WRITE → RUN_TESTS → PACKAGE_INSTALL → GIT_OPERATIONS → SYSTEM_COMMAND`.
Proje dizini dışına yazma, disk biçimlendirme, güvenlik yazılımı kapatma,
kimlik deposu okuma gibi işlemler **hiçbir seviyede** yürütülmez. İzin verilen,
engellenen ve onay bekleyen her komut kalıcı olarak kaydedilir ve denetim
kaydına `is_ai_action=True` ile yazılır.

---

## 8. Ölçeklenebilirlik / Scalability

Hedef senaryo: 1.000.000+ satış satırı, 100.000 müşteri, 10.000 ürün, 1.000 plasiyer.

- **İndeksleme:** 535 indeks; her sıcak sorgu yolu (tarih+özne, depo+ürün,
  müşteri+tarih, referans) bileşik indeksle karşılanır.
- **Sayfalama:** tüm liste uçları zorunlu `page`/`size` (üst sınır 500).
- **Ön-toplama:** `kpi_snapshots` günlük özet tablosu — panel 12 aylık grafiği
  milyonlarca satırı taramadan çizer; ham tablolardan her an yeniden üretilebilir.
- **Malzemelenmiş bakiye:** stok sorgusu defteri taramaz.
- **Bağlantı havuzu:** varsayılan 10 + 20 taşma.
- **SQLite ayarları:** WAL, `synchronous=NORMAL`, 64 MB sayfa önbelleği,
  30 sn meşgul zaman aşımı, yabancı anahtarlar açık.

**Ne zaman PostgreSQL'e geçilmeli:** eşzamanlı yazar sayısı ~20'yi aştığında
veya satış satırı 5M'yi geçtiğinde. Geçiş `VS_DATABASE_URL` değiştirip
`alembic upgrade head` çalıştırmaktan ibarettir — ORM kodu taşınabilir yazıldı
(JSONB, ARRAY, ILIKE ve diğer PostgreSQL'e özgü yapılar kullanılmadı).

---

## 9. Uluslararasılaştırma / i18n

Arayüz metni kodda **hard-code edilmez**. Servisler i18n *anahtarı* fırlatır
(`raise NotFoundError("customer.not_found")`), API katmanı çağıranın diline
göre çevirir. Dil çözümü: `?lang=` → `Accept-Language` → kullanıcı tercihi →
varsayılan (`tr`).

Kataloglar: `backend/app/locales/{tr,en}.json` ve `frontend/src/locales/{tr,en}.json`.
`app.core.i18n.missing_keys()` iki katalog arasındaki boşlukları raporlar.

---

## 10. Reddedilen alternatifler / Rejected alternatives

| Alternatif | Neden reddedildi |
|---|---|
| Async SQLAlchemy | SQLite'ta gerçek eşzamanlılık kazancı yok; sync kod hata ayıklaması çok daha basit. FastAPI zaten sync handler'ları thread havuzunda çalıştırır. |
| Zorunlu PostgreSQL + Redis + Docker | Hedef makinede hiçbiri kurulu değil. Kurulum sürtünmesi, çalışmayan bir sistemin en yaygın sebebidir. İkisi de opsiyonel olarak desteklenir. |
| `passlib[bcrypt]` | passlib 1.7.4, bcrypt ≥ 4.1 ile bozuk (kaldırılan `bcrypt.__about__` okunuyor). Doğrudan `bcrypt` kullanıldı, PBKDF2 yedeği eklendi. |
| prophet / statsmodels / scikit-learn | Windows'ta ağır derleme; kesintili FMCG talebinde SBA/TSB'ye üstünlük sağlamıyorlar. |
| Yalnızca OR-Tools ile rota | 100 MB bağımlılık kurulu değilse özellik tamamen kaybolurdu. Bağımsız yedek çözücü eklendi. |
| GPL/AGPL kod devşirme | Ürün özel dağıtıma kapanırdı. ERPNext/Odoo/OpenBoxes yalnızca **mimari olarak** incelendi; kod, şema metni veya veri alınmadı. |
| Rol izinlerini yalnızca veritabanında tutmak | Kod ile veri ayrışır. Katalog kodda tek kaynak, veritabanı ondan tohumlanır. |

---

## 11. Doğrulanan durum / Verified status

Bu belgedeki iddialar bu makinede fiilen çalıştırılarak doğrulanmıştır:

- 71 ORM tablosu, 1429 sütun, 535 indeks — gerçek SQLite veritabanına yaratıldı
- Alembic `upgrade → downgrade → upgrade` tam turu başarılı (72 tablo)
- Giriş, JWT, izin kataloğunun tamamı ve TR/EN hata mesajları uçtan uca test edildi
- LM Studio `:1234` — 6 model listelendi, sohbet tamamlama yanıt verdi
- NVIDIA `integrate.api.nvidia.com` — 102 model listelendi

Ayrıntılı ve güncel durum için `README.md` ve final raporuna bakınız.
