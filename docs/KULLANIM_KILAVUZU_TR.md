# Kullanım Kılavuzu

**Akıllı Sıcak Satış Yönetim Sistemi — v1.0.0**

Bu kılavuz sistemi kurmaktan günlük saha operasyonuna kadar tüm adımları anlatır.
Uygulama içinde **Sistem → Eğitim Merkezi** altında aynı konuları adım adım
gösteren 14 derslik interaktif bir modül de bulunur.

---

## İçindekiler

1. [Kurulum](#1-kurulum)
2. [İlk çalıştırma](#2-ilk-çalıştırma)
3. [Kullanıcı ve rol yönetimi](#3-kullanıcı-ve-rol-yönetimi)
4. [Ürün yönetimi](#4-ürün-yönetimi)
5. [Depo ve stok](#5-depo-ve-stok)
6. [Müşteri yönetimi](#6-müşteri-yönetimi)
7. [Plasiyer ve araç](#7-plasiyer-ve-araç)
8. [Rota planlama](#8-rota-planlama)
9. [Araç yükleme](#9-araç-yükleme)
10. [Sıcak satış](#10-sıcak-satış)
11. [Tahsilat](#11-tahsilat)
12. [İade](#12-iade)
13. [Gün sonu mutabakatı](#13-gün-sonu-mutabakatı)
14. [Kampanyalar](#14-kampanyalar)
15. [Raporlar ve istatistik](#15-raporlar-ve-istatistik)
16. [Yapay zeka](#16-yapay-zeka)
17. [Yedekleme ve geri yükleme](#17-yedekleme-ve-geri-yükleme)
18. [Sistem ayarları](#18-sistem-ayarları)
19. [Sorun giderme](#19-sorun-giderme)

---

## 1. Kurulum

### Gereksinimler

| Bileşen | Sürüm | Zorunlu mu |
|---|---|---|
| Windows | 10 / 11 | Evet |
| Python | 3.11 veya üzeri | Evet |
| Node.js | 20 veya üzeri | Web arayüzü için |
| PostgreSQL | 14+ | **Hayır** — varsayılan SQLite |
| Redis | — | **Hayır** — opsiyonel |
| Docker | — | **Hayır** |

### Adımlar

1. PowerShell'i açın ve proje klasörüne gidin:

```powershell
cd <kurulum-dizini>
```

2. Kurulumu çalıştırın:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithDemoData
```

Kurulum şunları yapar:
- Python sanal ortamını (`.venv`) oluşturur
- Backend ve frontend paketlerini kurar
- Rastgele üretilmiş bir gizli anahtarla `.env` dosyası yazar
- Veritabanı şemasını oluşturur ve referans verileri yükler
- `-WithDemoData` verilmişse gerçekçi örnek veri üretir

> `-WithDemoData` parametresini vermezseniz sistem boş başlar.
> Demo veriyi sonradan da yükleyebilirsiniz (bkz. Bölüm 19).

3. Sistemi başlatın:

```powershell
.\start.bat
```

İki pencere açılır (backend ve frontend) ve tarayıcı otomatik olarak
`http://localhost:5173` adresine gider.

---

## 2. İlk çalıştırma

### Giriş

| Alan | Değer |
|---|---|
| Kullanıcı adı | `admin` |
| Şifre | `admin` (yalnızca ilk ve yerel giriş için) |

Bu geçici kurulum kimliği yalnızca ana bilgisayardan kullanılabilir ve ilk
girişte hemen değiştirilmelidir.

İlk girişte sistem şifrenizi değiştirmenizi ister. **Profilim → Şifre Değiştir**
ekranından değiştirin.

### Dil değiştirme

Sağ üstteki **TR / EN** düğmesi arayüzü, hata mesajlarını ve raporları anında
değiştirir. Tercih kullanıcı hesabınıza kaydedilir.

### Şirket bilgileri

**Sistem → Ayarlar → Genel** altından şirket adı, para birimi, saat dilimi ve
varsayılan KDV oranını girin. Bu bilgiler faturalarda ve raporlarda kullanılır.

---

## 3. Kullanıcı ve rol yönetimi

### Yeni kullanıcı

**Sistem → Kullanıcılar → Yeni**

| Alan | Açıklama |
|---|---|
| Kullanıcı adı | Girişte kullanılır, benzersiz olmalı |
| Ad Soyad | Ekranlarda görünen isim |
| Rol | Yetkileri belirler (aşağıdaki tablo) |
| Bölge | REGION kapsamlı roller için zorunlu |
| Şifre | En az 8 karakter, büyük+küçük harf ve rakam |

### Roller

| Rol | Veri kapsamı | Tipik kullanım |
|---|---|---|
| Sistem Yöneticisi | Tümü | Kurulum, kullanıcılar, yedekleme |
| Şirket Sahibi | Tümü | Tam görünürlük |
| Genel Müdür | Tümü | Yönetim panosu, tüm raporlar |
| Satış Müdürü | Tümü | Satış, saha, CRM, kampanya |
| Bölge Satış Müdürü | Kendi bölgesi | Bölge operasyonu |
| Saha Satış Şefi | Kendi ekibi | Plasiyer denetimi, sayım onayı |
| **Plasiyer** | Kendi kayıtları | **Sıcak satış, tahsilat, ziyaret** |
| Şoför | Kendi kayıtları | Rota ve araç görünürlüğü |
| Merchandiser | Kendi kayıtları | Ziyaret, raf düzeni |
| Depo Müdürü | Tümü | Stok, transfer, sayım |
| Depo Personeli | Kendi kayıtları | Yükleme, sayım girişi |
| Lojistik Personeli | Tümü | Rota, araç, transfer |
| Muhasebe | Tümü | Fatura, tahsilat, cari |
| Tahsilat Personeli | Tümü | Tahsilat ve risk |
| Pazarlama / Trade Marketing | Tümü | Kampanya, fiyat listesi |
| Satış Analisti | Tümü | Analitik ve tahmin |
| AI Yöneticisi | Tümü | AI sağlayıcı ve bütçe |
| Denetçi | Tümü (salt okunur) | Denetim ve uyum |

### Kişiye özel yetki

Rolün verdiği yetkiler yetmiyorsa **Sistem → Kullanıcılar → (kullanıcı) →
İzinler** ekranından tek tek ekleme/çıkarma yapabilirsiniz.

> Güvenlik kuralı: kendinizden üstün bir rol atayamaz ve sahip olmadığınız bir
> yetkiyi başkasına veremezsiniz. Sistem bunu engeller.

---

## 4. Ürün yönetimi

**Stok → Ürünler → Yeni**

### Zorunlu alanlar

- **Stok kodu (SKU)** — benzersiz
- **Ürün adı**
- **Baz birim** — stokun tutulduğu birim (genellikle ADET)
- **Satış birimi** — sahada kullanılan birim (genellikle KOLİ)
- **Kolideki adet** — örn. 24

> Sistem tüm stoğu **baz birimde** tutar. Plasiyer koli girer, sistem adede
> çevirir. Bu sayede koli tanımı değişse bile geçmiş stok bozulmaz.

### Fiyat ve vergi

| Alan | Anlamı |
|---|---|
| Alış fiyatı | Tedarikçiden alış |
| Maliyet | Kâr hesabında kullanılır |
| Satış fiyatı | Baz birim başına liste fiyatı |
| Tavsiye edilen perakende | Müşterinin satması beklenen fiyat |
| KDV oranı | % olarak |
| Maks. iskonto | Sahada verilebilecek üst sınır |

### Raf ömrü

Gıda ürünlerinde **raf ömrü (gün)** girin. Sistem lot oluştururken üretim
tarihine bu süreyi ekleyerek son kullanma tarihini hesaplar ve **FEFO**
sıralamasında kullanır.

**Minimum kalan raf ömrü** alanı, bu sürenin altındaki ürünlerin araca
yüklenmesini engeller.

### Barkod

Bir ürünün birden fazla barkodu olabilir (adet barkodu, koli barkodu).
Plasiyer barkod okuttuğunda sistem doğru birimi otomatik seçer.

---

## 5. Depo ve stok

### Depo tipleri

| Tip | Kullanım |
|---|---|
| Merkez Depo | Fabrikadan gelen ana stok |
| Bölge Deposu | Bölgesel dağıtım noktası |
| Ara Depo | Geçici aktarma |
| **Araç Deposu** | Her satış aracının kendi stoğu |
| Karantina | Hasarlı / bloke ürün |

> Araç bir depodur. Bu yüzden araçta da lot takibi, FEFO, sayım ve değerleme
> aynen çalışır.

### Stok girişi

**Stok → Depolar → (depo) → Stok → Giriş**

Lot takipli ürünlerde **lot numarası, üretim tarihi ve son kullanma tarihi**
girilmesi zorunludur.

### Transfer

**Stok → Transfer → Yeni**

Transfer üç aşamalıdır:
1. **Taslak** — satırlar girilir
2. **Sevk** — kaynak depodan düşer, yolda görünür
3. **Teslim al** — hedef depoya girer

Teslim alırken miktar farklı girilebilir; fark otomatik raporlanır.

### Sayım

**Stok → Sayım → Yeni**

Sistem mevcut miktarları getirir, siz sayılan miktarı girersiniz. **Onayla**
dediğinizde fark kadar düzeltme hareketi yazılır ve denetim kaydına düşer.

> Onaylanmış sayım geri alınamaz. Hata varsa yeni bir düzeltme sayımı yapın.

### SKT takibi

**Stok → Lot / SKT** ekranı, son kullanma tarihi yaklaşan ve geçmiş ürünleri
listeler. Eşik **Ayarlar → Stok → SKT Uyarı Günü** ile ayarlanır (varsayılan 30).

---

## 6. Müşteri yönetimi

**CRM → Müşteriler → Yeni**

### Önemli alanlar

| Bölüm | Alan | Neden önemli |
|---|---|---|
| Kimlik | Ünvan, ticari isim, vergi no | Faturada görünür |
| Konum | **GPS koordinatı** | Rota optimizasyonu ve ziyaret doğrulama |
| Ziyaret | Ziyaret günleri, sıklık, servis süresi | Rota oluşturma |
| Ticari | Kredi limiti, risk limiti, vade | Satışta otomatik kontrol |
| Fiyat | Fiyat listesi | Sahada uygulanacak fiyat |

> **GPS koordinatı girmezseniz** müşteri rota optimizasyonuna dahil edilemez ve
> ziyaret doğrulaması yapılamaz. Mobil cihazda "Konumumu kullan" düğmesi
> müşterinin önündeyken tek dokunuşla koordinatı alır.

### Kredi limiti

Kredi limiti **0** ise limit sınırsızdır. Sıfırdan büyükse, bakiye + yeni
sipariş tutarı limiti aşarsa satış engellenir.

### Cari hesap

**CRM → Müşteriler → (müşteri) → Cari Hesap** sekmesi tüm borç/alacak
hareketlerini yürüyen bakiye ile gösterir. **Ekstre** sekmesinden tarih aralığı
seçip PDF alabilirsiniz.

---

## 7. Plasiyer ve araç

### Araç tanımı

**Saha → Araçlar → Yeni**

Araç kaydedildiğinde sistem **otomatik olarak o araca ait bir depo oluşturur**.
Ayrıca depo tanımlamanıza gerek yoktur.

Kapasite alanları (hacim ve ağırlık) araç yüklemede kontrol edilir.

### Plasiyer tanımı

**Saha → Plasiyerler → Yeni**

Plasiyeri bir **kullanıcı hesabına** bağlayın; böylece sahadaki kişi kendi
adıyla giriş yapar ve yalnızca kendi verisini görür.

**Maks. iskonto** alanı, o plasiyerin sahada verebileceği üst sınırdır.

---

## 8. Rota planlama

### Şablon rota

**Saha → Rotalar → Yeni** ile haftanın bir gününe ait şablon rota oluşturun ve
müşterileri ekleyin. Şablonlar her hafta tekrarlanır.

### Günlük rota üretimi

**Saha → Rotalar → Günlük Rotaları Oluştur** düğmesi, müşterilerin ziyaret
günlerine bakarak o güne ait rotaları üretir.

### Optimizasyon

Rotayı açıp **Optimize Et** düğmesine basın. Sistem:
- müşteri koordinatlarını, araç kapasitesini, servis sürelerini ve çalışma
  saatlerini dikkate alır,
- durak sırasını yeniden düzenler,
- toplam mesafe ve süreyi hesaplar,
- hangi çözücünün kullanıldığını gösterir.

> OR-Tools kurulu ise kesin çözücü, değilse sistemin kendi Clarke-Wright +
> 2-opt çözücüsü kullanılır. **Her iki durumda da optimizasyon çalışır.**

### Planlanan / gerçekleşen

Gün sonunda **Plan-Gerçekleşen** sekmesi; tamamlanan, atlanan ve geciken
durakları, planlanan ve gerçekleşen km/süre farkını gösterir.

---

## 9. Araç yükleme

**Stok → Araç Yükleme**

1. Plasiyer, araç, tarih ve kaynak depoyu seçin
2. **AI Önerisi Al** düğmesine basın

Sistem şunlara bakarak öneri üretir:
- o günkü rotadaki müşterilerin geçmiş tüketimi,
- haftanın günü etkisi,
- son 4 haftalık eğilim,
- aktif kampanyalar,
- araçta hâlihazırda bulunan stok,
- araç hacim/ağırlık kapasitesi,
- depodaki mevcut stok ve kalan raf ömrü.

Her satırda **öneri gerekçesi** ve güven düzeyi gösterilir. Miktarları
değiştirebilir, satır ekleyip çıkarabilirsiniz.

3. **Kaydet** → **İşle**

İşlendiğinde stok depodan düşer, araca girer ve iki tarafta da hareket kaydı
oluşur.

---

## 10. Sıcak satış

**Satış → Sıcak Satış** — sistemin en çok kullanılan ekranı.

### Adımlar

1. **Müşteri seçin.** Ekranda müşterinin bakiyesi, kredi limiti, risk durumu
   ve son alışverişleri görünür.
2. **AI önerilerine bakın.** Sistem, müşterinin ortalama tüketimi ve son
   alışveriş tarihinden hareketle hangi ürünün bitmiş olma ihtimalinin yüksek
   olduğunu ve ne kadar önerileceğini gerekçesiyle gösterir.
3. **Sepeti kurun.** Araç stoğundan ürüne dokunun; miktar ve birim girin.
   Yetkiniz varsa satır iskontosu uygulayabilirsiniz.
4. **Kampanyalar otomatik uygulanır.** "10 al 1 bedava" gibi kampanyalarda
   bedava ürün ayrı satır olarak eklenir ve gerekçesi gösterilir.
5. **Ödemeyi girin.** Nakit, kredi kartı, havale, çek veya açık hesap.
6. **Satışı Tamamla.**

### Arka planda ne olur

Tek bir veritabanı işleminde:
sipariş → teslimat → araç stoğundan FEFO ile düşüm (hangi lot çıktığı kayıtlı)
→ fatura → cari hesap kaydı → tahsilat (varsa) → denetim kaydı.

Herhangi bir adım başarısız olursa **hiçbiri yazılmaz**; yarım satış oluşmaz.

### Çevrimdışı çalışma

Bağlantı yokken sepet cihazda tutulur. Her sepetin benzersiz bir kimliği vardır;
bağlantı gelince gönderilir. Aynı satış iki kez gönderilse bile sistem ikinciyi
yeni kayıt olarak işlemez.

---

## 11. Tahsilat

**Satış → Tahsilatlar → Yeni**

| Ödeme tipi | Davranış |
|---|---|
| Nakit / Kart / Havale | Bakiyeden hemen düşer |
| **Çek / Senet** | **Beklemede** olarak kaydedilir, bakiyeden düşmez |
| Açık hesap | Borç olarak kalır |

Çek tahsil edildiğinde **Tahsil Et**, karşılıksız çıktığında **Karşılıksız**
işaretleyin. Ancak tahsil edildiğinde bakiyeden düşer.

Tahsilat otomatik olarak **en eski açık faturadan** başlayarak dağıtılır;
istenirse fatura seçimi elle yapılabilir.

---

## 12. İade

**Satış → İadeler → Yeni**

Her satır için **sebep** ve **tasarruf kararı** girilir:

| Karar | Sonuç |
|---|---|
| Tekrar satılabilir | Stoğa geri girer |
| Fire / İmha | Fire hareketi yazılır, stoğa girmez |
| Karantina | Karantina statüsünde stoğa girer, satılamaz |

İade işlendiğinde talep edilirse iade faturası oluşur ve cari hesaba alacak
kaydedilir.

---

## 13. Gün sonu mutabakatı

**Saha → Gün Yönetimi**

### Gün açma

Sabah plasiyer aracı ve rotayı seçerek günü açar. Kilometre girilebilir.

### Gün kapatma

1. **Araç sayımı** yapılır — her üründen fiziken kaç adet kaldığı girilir
2. **Kasadaki nakit** beyan edilir
3. **Günü Kapat**

### Hesaplama

```
teorik = açılış + yüklenen + ek yükleme − satılan + iade − fire
fark   = teorik − sayılan
```

Fark sıfır değilse:
- gün oturumu **farklı** olarak işaretlenir,
- düzeltme hareketleri yazılır,
- denetim kaydına yazılır,
- yöneticiye bildirim gider.

Nakit farkı da aynı şekilde hesaplanır ve raporlanır.

> Bu ekran, "akşam sayımda 3 koli eksik çıktı" tartışmasının hakemi olan
> ekrandır. Tüm rakamlar hareket dökümünden gelir, elle tutulan bir sayaçtan
> değil.

---

## 14. Kampanyalar

**Pazarlama → Kampanyalar → Yeni**

### Desteklenen tipler

| Tip | Örnek |
|---|---|
| Al-Kazan (BUY_X_GET_Y) | 10 koli al, 1 koli bedava |
| Miktar iskontosu | 5 koli üzeri %5 |
| Tutar iskontosu | 20.000 TL üzeri %3 |
| Sepet karması | 3 farklı ürün alana ekstra iskonto |
| Sabit fiyat | Belirli müşteriye özel fiyat |
| Yüzde / tutar iskontosu | Genel indirim |

### Kapsam

Kampanya tümüne, belirli müşteriye, müşteri tipine, kanala, bölgeye, rotaya,
plasiyere, ürüne, kategoriye veya markaya uygulanabilir.

### Test etme

**Önizleme** panelinden örnek bir sepet girip kampanyanın ne vereceğini
kaydetmeden görebilirsiniz.

### Kârlılık

Kampanya detayındaki **ROI** sekmesi verilen iskontoyu, bedava mal maliyetini,
elde edilen ciroyu ve net etkiyi gösterir.

---

## 15. Raporlar ve istatistik

### Raporlar

**Analitik → Raporlar** — 21 hazır rapor: günlük/haftalık/aylık/yıllık satış,
plasiyer, müşteri, SKU, marka, kategori, bölge, rota performansı, tahsilat,
cari risk, stok, araç stok, SKT, fire, iade, kampanya, kârlılık, hedef.

Her rapor **PDF**, **Excel** ve **CSV** olarak indirilebilir.

> Excel ve CSV çıktıları Türkçe karakterler için UTF-8 BOM ile üretilir;
> Excel'de doğrudan düzgün açılır.

### İstatistik

**Analitik → İstatistik** ekranı ortalama, medyan, mod, standart sapma,
varyans, çeyrekler, yüzdelikler; zaman serisi trendi, hareketli ortalama,
WoW/MoM/YoY değişim; korelasyon matrisi ve regresyon analizi sunar.

### Tahmin

**Analitik → Tahminler** ekranında ürün, müşteri veya plasiyer seçip ileriye
dönük talep tahmini alabilirsiniz.

Sistem seriyi önce sınıflandırır (düzgün / kesintili / düzensiz) ve yöntemi ona
göre seçer. Seçim geriye dönük testle doğrulanır; ekranda kullanılan yöntem,
hata payı ve güven aralığı gösterilir.

---

## 16. Yapay zeka

### Sağlayıcı ayarları

**Yapay Zeka → AI Sağlayıcıları**

| Sağlayıcı | Kurulum |
|---|---|
| **LM Studio** | Yerel, ücretsiz. LM Studio'da sunucuyu başlatın, adres `http://localhost:1234/v1` |
| **NVIDIA** | `.env` içine `VS_NVIDIA_API_KEY` ekleyin |
| **Claude** | `.env` içine `VS_CLAUDE_API_KEY` ekleyin ve `VS_CLAUDE_ENABLED=true` yapın |

Her sağlayıcı için **Bağlantıyı Test Et** düğmesi gerçek bir çağrı yapar ve
gecikmeyi gösterir.

> API anahtarları veritabanına **kaydedilmez**. Sadece "anahtar tanımlı mı"
> bilgisi tutulur. Ekranda anahtar maskeli görünür.

### AI Satış Müdürü

**Yapay Zeka → AI Satış Müdürü** — doğal Türkçe soru sorun:

- "Bugün en fazla satış yapan 10 plasiyeri getir"
- "Son 30 günde satışları düşen müşterileri bul"
- "Tahsilat riski yüksek müşterileri göster"
- "En kârlı 20 ürünü getir"
- "Son 90 günde kaybettiğimiz müşterileri bul"

Sistem soruyu **salt okunur** bir sorguya çevirir, çalıştırır ve sonucu
yorumlar. Kullanılan veri kaynağı "Kaynak veri" bölümünden görülebilir.

> Güvenlik: yalnızca tek bir okuma sorgusu çalıştırılabilir. Veri değiştiren
> hiçbir komut kabul edilmez; kullanıcı, oturum ve denetim tabloları erişime
> kapalıdır.

### AI Plasiyer Asistanı

Müşteri bazlı sipariş önerisi ve araç yükleme önerisi üretir. Her öneri
gerekçesiyle birlikte gelir.

### Token ve maliyet

**Yapay Zeka → Token / Maliyet** ekranı günlük ve aylık token kullanımını,
tahmini maliyeti ve bütçe doluluğunu gösterir. Bütçe aşıldığında **ücretli**
sağlayıcılar durur; **yerel model çalışmaya devam eder**.

---

## 17. Yedekleme ve geri yükleme

**Sistem → Yedekleme**

### Yedek alma

**Şimdi Yedekle** düğmesi tam yedek alır. Yedek:
- veritabanının tutarlı bir kopyasını içerir,
- SHA-256 ile damgalanır,
- sıkıştırılarak `backups/` klasörüne yazılır.

Otomatik yedekleme **Ayarlar → Yedekleme** altından günlük/haftalık/aylık
olarak ayarlanabilir.

### Doğrulama

**Doğrula** düğmesi yedeğin sağlamlığını kontrol eder. Doğrulanmamış bir yedek
güvence değildir; düzenli olarak doğrulayın.

### Geri yükleme

**Geri Yükle** iki aşamalı onay ister. Sistem:
1. önce yedeği doğrular,
2. mevcut veritabanının **güvenlik yedeğini** alır,
3. sonra geri yükler.

> Geri yükleme mevcut veriyi değiştirir. İşlem öncesi otomatik güvenlik yedeği
> alındığı için hatalı bir geri yükleme de geri alınabilir.

---

## 18. Sistem ayarları

**Sistem → Ayarlar** — kategorilere ayrılmıştır:

| Kategori | Örnek ayarlar |
|---|---|
| Genel | Şirket adı, dil, para birimi, saat dilimi |
| Satış | Varsayılan KDV, kredi limiti zorunluluğu, saha maks. iskonto |
| Stok | FEFO/FIFO, SKT uyarı günü, sayım fark toleransı |
| Rota | Ortalama hız, yol sapma katsayısı, ziyaret yarıçapı |
| AI | Yedekleme sırası, aylık bütçe, terminal varsayılan yetkisi |
| Yedekleme | Otomatik yedek, sıklık, saklama süresi |

### Sistem sağlığı

**Sistem → Sistem Sağlığı** dokuz bileşeni kontrol eder: backend, veritabanı,
Redis, LM Studio, NVIDIA, Claude, disk, yedekleme ve kuyruk. Her biri
**OK / UYARI / HATA / BİLİNMİYOR** durumu gösterir.

### Denetim kaydı

**Sistem → Denetim Kaydı** tüm kritik işlemleri listeler. **Zinciri Doğrula**
düğmesi kayıtların değiştirilip değiştirilmediğini kontrol eder — geçmiş bir
satır düzenlenmişse sistem bunu tespit eder ve yerini bildirir.

---

## 19. Sorun giderme

### Sistem açılmıyor

```powershell
.\.venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'backend');from app.main import app;print('OK')"
```

Hata mesajını `logs\error.log` dosyasında da bulabilirsiniz.

### Giriş yapılamıyor

Hesap 5 başarısız denemeden sonra 15 dakika kilitlenir. Beklemek istemiyorsanız
başka bir yönetici hesabıyla **Sistem → Kullanıcılar → (kullanıcı) → Durum:
Aktif** yapın.

### LM Studio bağlanmıyor

1. LM Studio açık mı?
2. **Developer / Local Server → Start Server** yapıldı mı?
3. Bir model yüklendi mi?
4. **AI Sağlayıcıları → LM Studio → Bağlantıyı Test Et**

### Türkçe karakterler bozuk

Excel/CSV çıktıları UTF-8 BOM ile üretilir. Sorun devam ederse Excel'de
**Veri → Metinden/CSV'den** yolunu kullanıp kodlamayı **UTF-8** seçin.

### Demo veriyi yeniden yükleme

```powershell
cd <kurulum-dizini>\backend
..\.venv\Scripts\python.exe -m scripts.seed_demo_data --reset
```

> `--reset` mevcut demo verisini siler. Gerçek veriyle çalışıyorsanız
> **bu komutu çalıştırmayın**; önce yedek alın.

### Veritabanını PostgreSQL'e taşıma

1. PostgreSQL kurun ve boş bir veritabanı oluşturun
2. `.venv\Scripts\python.exe -m pip install "psycopg[binary]"`
3. `.env` içindeki `VS_DATABASE_URL` satırını değiştirin:
   `postgresql+psycopg://kullanici:sifre@localhost:5432/van_sales`
4. `cd backend && ..\.venv\Scripts\python.exe -m alembic upgrade head`

### Loglar

| Dosya | İçerik |
|---|---|
| `logs\application.log` | Genel işlem kaydı |
| `logs\error.log` | Yalnızca hatalar |
| `logs\ai.log` | Yapay zeka çağrıları |
| `logs\security.log` | Giriş, yetki, denetim |

> Loglara API anahtarı, parola veya jeton **yazılmaz**; otomatik olarak
> temizlenir.

---

## Destek

Teknik ayrıntılar için `ARCHITECTURE.md`, sürüm geçmişi için `CHANGELOG.md`,
üçüncü taraf lisansları için `THIRD_PARTY_NOTICES.md` dosyalarına bakınız.
