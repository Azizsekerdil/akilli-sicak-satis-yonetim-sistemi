"""
In-app training centre (Eğitim Merkezi).

The 14 lessons below are the operator manual, kept in the database rather than
in a PDF so the app can deep-link a user straight from a screen into the lesson
that explains it, and so completion can be tracked per user.

Content is authored bilingually here (Turkish first — that is the primary
language of the field staff) and seeded idempotently: re-running
:func:`seed_lessons` refreshes the text of existing lessons without touching
anyone's progress.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import RoleCode
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging_config import get_logger
from app.core.utils import clamp, dumps, loads
from app.models.base import utcnow
from app.models.system import TrainingLesson, TrainingProgress
from app.services import auth_service

log = get_logger("app.training")


def _step(
    title_tr: str, title_en: str, detail_tr: str, detail_en: str, screen: str | None = None
) -> dict[str, Any]:
    return {
        "title_tr": title_tr,
        "title_en": title_en,
        "detail_tr": detail_tr,
        "detail_en": detail_en,
        "screen": screen,
    }


# ===========================================================================
# Lesson content
# ===========================================================================
LESSONS: list[dict[str, Any]] = [
    {
        "code": "01",
        "module": "system",
        "target_route": "/login",
        "estimated_minutes": 5,
        "title_tr": "Sisteme Giriş",
        "title_en": "Getting Started & Signing In",
        "summary_tr": "Kullanıcı adı ve şifre ile giriş, dil seçimi, şifre değiştirme ve oturum güvenliği.",
        "summary_en": "Signing in, choosing your language, changing your password and session safety.",
        "body_tr": (
            "Sisteme her kullanıcı kendi hesabıyla girer. Hesabınız rolünüze göre yetkilendirilir: "
            "bir plasiyer yalnızca kendi müşterilerini ve kendi aracının stoğunu görürken, bölge "
            "müdürü bölgesindeki tüm plasiyerleri görür.\n\n"
            "İlk girişte sistem sizden şifrenizi değiştirmenizi ister. Şifreniz en az 8 karakter "
            "olmalı, büyük harf, küçük harf ve rakam içermelidir. Şifrenizi kimseyle paylaşmayın; "
            "yaptığınız her satış, iskonto ve tahsilat denetim kaydına adınızla yazılır.\n\n"
            "Arka arkaya 5 hatalı denemeden sonra hesabınız 15 dakika kilitlenir. Bu, şifre deneme "
            "saldırılarına karşı bir korumadır. Kilitlenirseniz yöneticinizden kilidi açmasını "
            "isteyebilirsiniz.\n\n"
            "Sağ üstteki dil seçicisinden Türkçe ve İngilizce arasında geçiş yapabilirsiniz; seçiminiz "
            "hesabınıza kaydedilir. Gün sonunda mutlaka 'Çıkış' yapın — özellikle ortak kullanılan "
            "bir tablet veya el terminalinde."
        ),
        "body_en": (
            "Everyone signs in with their own account. Your account is authorised by role: a "
            "salesperson sees only their own customers and van stock, while a regional manager sees "
            "every salesperson in their region.\n\n"
            "On first sign-in the system asks you to change your password. It must be at least 8 "
            "characters and contain an upper-case letter, a lower-case letter and a digit. Never "
            "share it: every sale, discount and collection you make is written to the audit log "
            "under your name.\n\n"
            "After 5 consecutive failed attempts the account locks for 15 minutes. This protects "
            "against password-guessing. If you get locked out, ask your manager to unlock you.\n\n"
            "Use the language switch at the top right to move between Turkish and English; your "
            "choice is saved to your account. Always sign out at the end of the day, especially on "
            "a shared tablet or handheld."
        ),
        "steps": [
            _step("Giriş ekranını açın", "Open the sign-in screen",
                  "Tarayıcıda uygulama adresini açın ve kullanıcı adınızı yazın.",
                  "Open the application address in the browser and type your username.", "/login"),
            _step("Şifrenizi girin", "Enter your password",
                  "Şifre alanına şifrenizi yazın ve 'Giriş Yap' düğmesine basın.",
                  "Type your password and press 'Sign in'.", "/login"),
            _step("İlk şifrenizi değiştirin", "Change your initial password",
                  "Sistem sizi şifre değiştirme ekranına yönlendirir; yeni şifrenizi belirleyin.",
                  "The system takes you to the change-password screen; set your new password.",
                  "/profile/password"),
            _step("Dilinizi seçin", "Choose your language",
                  "Sağ üstteki bayrak simgesinden Türkçe veya İngilizce seçin.",
                  "Pick Turkish or English from the flag icon at the top right.", None),
            _step("Çıkış yapın", "Sign out",
                  "Gün sonunda profil menüsünden 'Çıkış' seçeneğine tıklayın.",
                  "At the end of the day choose 'Sign out' from the profile menu.", None),
        ],
    },
    {
        "code": "02",
        "module": "dashboard",
        "target_route": "/dashboard",
        "estimated_minutes": 6,
        "title_tr": "Kontrol Paneli ve Göstergeler",
        "title_en": "Dashboard & Key Indicators",
        "summary_tr": "Günlük ciro, tahsilat, ziyaret ve stok göstergelerini okumak; grafikleri filtrelemek.",
        "summary_en": "Reading the daily revenue, collection, visit and stock indicators; filtering charts.",
        "body_tr": (
            "Kontrol paneli, işin o anki durumunu tek ekranda gösterir. Üst sıradaki kartlar günün "
            "cirosunu, tahsilatını, ziyaret sayısını ve kâr marjını; yanlarındaki küçük yüzdeler ise "
            "bir önceki döneme göre değişimi gösterir. Yeşil artış, kırmızı azalış demektir.\n\n"
            "Grafikler tıklanabilir: bir bölgeye tıkladığınızda alttaki listeler o bölgeye göre "
            "filtrelenir. Tarih aralığını değiştirmek tüm paneli yeniden hesaplar.\n\n"
            "Panelde gördüğünüz veriler yetkinizle sınırlıdır. Plasiyer kendi rakamlarını, şef ekibinin "
            "toplamını, genel müdür şirketin tamamını görür. Bu yüzden iki kullanıcının aynı ekranda "
            "farklı sayılar görmesi normaldir.\n\n"
            "Sağdaki 'Dikkat Gerektirenler' bölümü, stok azalması, SKT yaklaşması, vadesi geçen alacak "
            "ve hedef riski gibi konuları öne çıkarır; buradaki her satır ilgili ekrana bağlantı içerir."
        ),
        "body_en": (
            "The dashboard shows the current state of the business on one screen. The top cards show "
            "today's revenue, collections, visit count and margin; the small percentages next to them "
            "show the change versus the previous period. Green is up, red is down.\n\n"
            "Charts are clickable: selecting a region filters the lists below it. Changing the date "
            "range recalculates the whole panel.\n\n"
            "What you see is limited by your permissions. A salesperson sees their own numbers, a "
            "supervisor their team's, a general manager the whole company. Two users seeing different "
            "numbers on the same screen is expected.\n\n"
            "The 'Needs attention' panel on the right surfaces low stock, approaching expiry dates, "
            "overdue receivables and targets at risk; every line links to the relevant screen."
        ),
        "steps": [
            _step("Tarih aralığını seçin", "Select the date range",
                  "Sağ üstteki tarih seçiciden gün, hafta veya ay aralığını belirleyin.",
                  "Use the date picker at the top right to choose a day, week or month.", "/dashboard"),
            _step("Kartları okuyun", "Read the KPI cards",
                  "Ciro, tahsilat, ziyaret ve marj kartlarındaki değişim yüzdelerini inceleyin.",
                  "Review the change percentages on the revenue, collection, visit and margin cards.",
                  "/dashboard"),
            _step("Grafiği filtreleyin", "Filter the chart",
                  "Bölge veya kanal grafiğinde bir dilime tıklayarak listeyi daraltın.",
                  "Click a slice of the region or channel chart to narrow the list.", "/dashboard"),
            _step("Uyarıları takip edin", "Follow the alerts",
                  "'Dikkat Gerektirenler' listesindeki bir satıra tıklayıp ilgili ekrana gidin.",
                  "Click a line in 'Needs attention' to jump to the relevant screen.", "/dashboard"),
        ],
    },
    {
        "code": "03",
        "module": "crm",
        "target_route": "/crm/customers",
        "estimated_minutes": 10,
        "title_tr": "Müşteri Yönetimi",
        "title_en": "Customer Management",
        "summary_tr": "Müşteri kartı açmak, konum ve ziyaret günü tanımlamak, kredi limiti ve cari hesabı okumak.",
        "summary_en": "Creating customer cards, setting location and visit days, reading credit limit and account.",
        "body_tr": (
            "Müşteri kartı, satışın temelidir: fiyat listesi, iskonto, ödeme vadesi, kredi limiti ve "
            "ziyaret planı hep burada tanımlıdır. Yeni müşteri açarken ünvan, vergi dairesi/numarası ve "
            "adres alanlarını eksiksiz doldurun — fatura bu bilgilerle basılır.\n\n"
            "Konum çok önemlidir. 'Haritadan Seç' veya sahadayken 'Mevcut Konumu Kullan' ile koordinat "
            "kaydedin. Koordinatı olmayan müşteri rota optimizasyonuna giremez ve ziyaret doğrulaması "
            "(geofence) yapılamaz.\n\n"
            "Ziyaret sıklığı ve ziyaret günleri, rota şablonlarının hangi müşteriyi hangi güne koyacağını "
            "belirler. Haftada iki kez ziyaret edilen bir bakkal için 'İki Haftada Bir' değil 'Haftada "
            "İki' seçilmelidir.\n\n"
            "Kredi limiti ve risk limiti, açık hesap satışını kısıtlar. Limit aşılırsa sistem satışı "
            "engeller; bu bir hata değil, bilinçli bir iş kuralıdır. Limit değişikliği yalnızca yetkili "
            "rollerce yapılabilir ve denetim kaydına yazılır."
        ),
        "body_en": (
            "The customer card is the foundation of selling: price list, discount, payment terms, "
            "credit limit and the visit plan all live here. When creating a customer, complete the "
            "legal name, tax office/number and address — invoices are printed from these fields.\n\n"
            "Location matters. Save coordinates with 'Pick on map' or, in the field, 'Use current "
            "location'. A customer without coordinates cannot enter route optimisation and visit "
            "verification (geofence) cannot work.\n\n"
            "Visit frequency and visit days decide which day a route template places the customer on. "
            "A grocery visited twice a week must be set to 'Twice weekly', not 'Biweekly'.\n\n"
            "Credit and risk limits constrain open-account selling. When a limit would be exceeded the "
            "system blocks the sale; that is a deliberate business rule, not an error. Only authorised "
            "roles may change a limit, and the change is written to the audit log."
        ),
        "steps": [
            _step("Yeni müşteri açın", "Create a customer",
                  "Müşteriler ekranında '+ Yeni' düğmesine basın ve ünvan/vergi bilgilerini girin.",
                  "Press '+ New' on the Customers screen and enter the legal and tax details.",
                  "/crm/customers/new"),
            _step("Konumu kaydedin", "Save the location",
                  "Haritadan işaretleyin veya sahadayken mevcut konumu kullanın.",
                  "Mark it on the map, or capture the current location while on site.", None),
            _step("Ziyaret planını tanımlayın", "Define the visit plan",
                  "Ziyaret sıklığı, ziyaret günleri ve servis süresini girin.",
                  "Set the visit frequency, visit days and service time.", None),
            _step("Ticari koşulları girin", "Set the commercial terms",
                  "Fiyat listesi, ödeme yöntemi, vade ve kredi limitini belirleyin.",
                  "Choose the price list, payment method, term and credit limit.", None),
            _step("Cari hesabı inceleyin", "Review the current account",
                  "Müşteri kartındaki 'Cari Hesap' sekmesinden borç, alacak ve vade dağılımını görün.",
                  "Open the 'Current account' tab to see debit, credit and ageing.",
                  "/crm/customers"),
        ],
    },
    {
        "code": "04",
        "module": "stock",
        "target_route": "/stock/products",
        "estimated_minutes": 9,
        "title_tr": "Ürün ve Fiyat Yönetimi",
        "title_en": "Products & Pricing",
        "summary_tr": "Ürün kartı, birim dönüşümleri (koli/adet), barkod, KDV ve fiyat listeleri.",
        "summary_en": "Product cards, unit conversions (case/piece), barcodes, VAT and price lists.",
        "body_tr": (
            "Her ürün bir temel birimde (genelde ADET) stoklanır; koli, paket ve palet bu temel birime "
            "çevrilerek işlenir. '1 koli = 24 adet' tanımını doğru girmek kritiktir: yanlış katsayı, "
            "araç stoğunda ve sayımda doğrudan fark yaratır.\n\n"
            "Barkodlar ürün-birim çiftine bağlıdır. Koli barkodu ile adet barkodu ayrı satırlardır; "
            "sahada okutulan barkod hangi satıra aitse satış o birimde açılır.\n\n"
            "KDV oranı ürün kartında tutulur ve satırda kullanılır. ÖTV alanı, düzenlemeye tabi ürünler "
            "için hazır bırakılmıştır.\n\n"
            "Fiyat listeleri kanal, bölge veya müşteri tipine göre tanımlanır ve öncelik sırasına göre "
            "uygulanır. Müşteri kartında özel bir fiyat listesi seçiliyse o kazanır. Sahada fiyat "
            "değiştirme yetkisi ayrı bir izindir ve maksimum iskonto oranı ürün kartındaki sınırı aşamaz."
        ),
        "body_en": (
            "Every product is stocked in a base unit (usually PIECE); cases, packs and pallets are "
            "converted to that base. Getting '1 case = 24 pieces' right is critical: a wrong factor "
            "creates real variance in van stock and counts.\n\n"
            "Barcodes belong to a product-unit pair. The case barcode and the piece barcode are "
            "separate rows; whichever is scanned in the field determines the selling unit.\n\n"
            "The VAT rate lives on the product card and is applied per line. The excise (ÖTV) field is "
            "there for regulated goods.\n\n"
            "Price lists are defined per channel, region or customer type and applied by priority. If "
            "the customer card names a specific price list, that one wins. Changing price in the field "
            "is a separate permission, and discounts can never exceed the product's maximum."
        ),
        "steps": [
            _step("Ürün kartı açın", "Create a product",
                  "Stok kodu (SKU), ad, kategori ve marka bilgilerini girin.",
                  "Enter the SKU, name, category and brand.", "/stock/products/new"),
            _step("Birimleri tanımlayın", "Define the units",
                  "Temel birimi ve koli katsayısını (1 koli = kaç adet) girin.",
                  "Set the base unit and the case factor (units per case).", None),
            _step("Barkodları ekleyin", "Add barcodes",
                  "Adet ve koli barkodlarını ayrı satırlar olarak kaydedin.",
                  "Save the piece and case barcodes as separate rows.", None),
            _step("Fiyat ve KDV girin", "Enter price and VAT",
                  "Satış fiyatı, maliyet, KDV oranı ve maksimum iskontoyu belirleyin.",
                  "Set the sale price, cost, VAT rate and maximum discount.", None),
            _step("Fiyat listesine ekleyin", "Add to a price list",
                  "Kanal veya bölgeye özel fiyat listesine ürünü ekleyin.",
                  "Add the product to the channel- or region-specific price list.",
                  "/marketing/price-lists"),
        ],
    },
    {
        "code": "05",
        "module": "stock",
        "target_route": "/stock/warehouses",
        "estimated_minutes": 10,
        "title_tr": "Depo ve Stok Yönetimi",
        "title_en": "Warehouse & Stock Management",
        "summary_tr": "Depo tipleri, parti (lot) ve SKT takibi, FEFO çıkış, transfer ve sayım.",
        "summary_en": "Warehouse types, lot and expiry tracking, FEFO issue, transfers and counts.",
        "body_tr": (
            "Stok hareketleri değiştirilemez bir defterde tutulur: her giriş ve çıkış ayrı bir satırdır, "
            "düzeltmeler silme ile değil ters hareketle yapılır. Bakiyeler bu defterden türetilir, bu "
            "yüzden 'stok neden şu kadar' sorusunun cevabı her zaman izlenebilir.\n\n"
            "Depo tipleri: merkez, bölge, ara depo, araç ve karantina. Araç da bir depodur — bu sayede "
            "araçtaki mal için de aynı stok kuralları çalışır.\n\n"
            "Gıda ve içecekte parti (lot) ve son kullanma tarihi zorunludur. Çıkış varsayılan olarak "
            "FEFO'dur: son kullanma tarihi en yakın parti önce çıkar. SKT'si geçmiş parti satılamaz; "
            "sistem bunu engeller.\n\n"
            "Transfer, iki depo arasında sevk ve kabul olarak iki adımda çalışır: mal yola çıktığında "
            "kaynak depodan düşer, kabul edildiğinde hedef depoya girer. Sayım ise teorik ile fiziki "
            "arasındaki farkı ortaya çıkarır ve onaylandığında düzeltme hareketi üretir."
        ),
        "body_en": (
            "Stock movements are kept in an immutable ledger: every receipt and issue is its own row, "
            "and corrections are posted as reversing movements rather than deletions. Balances are "
            "derived from that ledger, so 'why is stock this number' is always answerable.\n\n"
            "Warehouse types: central, regional, transit, vehicle and quarantine. A vehicle is a "
            "warehouse too — so the same stock rules apply to goods on the van.\n\n"
            "In food and beverage, lot and expiry tracking are mandatory. Issue is FEFO by default: "
            "the earliest-expiring lot leaves first. Expired lots cannot be sold; the system blocks it.\n\n"
            "A transfer is two steps — ship and receive: stock leaves the source when despatched and "
            "enters the target when accepted. A count exposes the difference between theoretical and "
            "physical stock and, once approved, posts the adjustment movement."
        ),
        "steps": [
            _step("Depoyu tanımlayın", "Define the warehouse",
                  "Depo tipini, adresini ve eksi stok politikasını belirleyin.",
                  "Set the warehouse type, address and negative-stock policy.",
                  "/stock/warehouses/new"),
            _step("Parti ile mal girişi yapın", "Receive goods with a lot",
                  "Parti numarası, üretim ve son kullanma tarihi ile giriş kaydedin.",
                  "Record a receipt with lot number, production and expiry dates.", "/stock/lots"),
            _step("Transfer oluşturun", "Create a transfer",
                  "Kaynak ve hedef depoyu seçip satırları girin, sevk edin.",
                  "Pick source and target warehouses, add lines and despatch.", "/stock/transfers/new"),
            _step("Transferi kabul edin", "Receive the transfer",
                  "Hedef depoda gelen miktarları doğrulayıp kabul edin.",
                  "Confirm the received quantities at the target warehouse.", "/stock/transfers"),
            _step("Sayım yapın", "Run a count",
                  "Sayım belgesi açın, fiziki miktarları girin ve onaylayın.",
                  "Open a count document, enter physical quantities and approve.", "/stock/counts/new"),
        ],
    },
    {
        "code": "06",
        "module": "stock",
        "target_route": "/stock/van-load",
        "estimated_minutes": 8,
        "title_tr": "Araç Yükleme",
        "title_en": "Van Loading",
        "summary_tr": "Sabah yüklemesi, AI yükleme önerisi, kapasite kontrolü ve gün içi takviye.",
        "summary_en": "Morning load-out, AI load suggestions, capacity checks and intraday top-ups.",
        "body_tr": (
            "Gün, aracın yüklenmesiyle başlar. Yükleme belgesi, merkez veya bölge deposundan araç "
            "deposuna bir transferdir; onaylandığında mal fiziksel olarak araca, kayıt olarak araç "
            "deposuna geçer.\n\n"
            "Sistem, rotadaki müşterilerin geçmiş satışlarına, mevsimselliğe ve açık siparişlere bakarak "
            "yükleme önerisi üretir. Öneri bir tahmindir: plasiyer miktarları değiştirebilir. Değiştirilen "
            "satırlar kaydedilir ve tahmin doğruluğunun ölçülmesinde kullanılır.\n\n"
            "Araç kapasitesi hacim (litre) ve ağırlık (kg) olarak kontrol edilir. Kapasite aşılırsa sistem "
            "uyarır; soğutmalı araç gerektiren ürünleri normal araca yüklemek de engellenir.\n\n"
            "Gün içinde stok biterse 'takviye yükleme' yapılır. Takviye, sabah yüklemesinden ayrı işaretlenir; "
            "gün sonu mutabakatında ikisi ayrı ayrı görünür."
        ),
        "body_en": (
            "The day starts by loading the van. A load-out is a transfer from a central or regional "
            "warehouse into the vehicle warehouse; once posted, the goods are physically on the van and "
            "recorded in the van's stock.\n\n"
            "The system proposes a load from the route's customer history, seasonality and open orders. "
            "The proposal is a forecast: the salesperson may change quantities. Changed lines are saved "
            "and used to measure forecast accuracy.\n\n"
            "Vehicle capacity is checked in volume (litres) and weight (kg). Exceeding it raises a "
            "warning, and loading chilled-only products onto a non-refrigerated van is blocked.\n\n"
            "If stock runs out during the day, a top-up load is made. Top-ups are flagged separately "
            "from the morning load and appear separately in the end-of-day reconciliation."
        ),
        "steps": [
            _step("Yükleme belgesi açın", "Open a load document",
                  "Aracı, plasiyeri ve kaynak depoyu seçin.",
                  "Choose the vehicle, salesperson and source warehouse.", "/stock/van-load/new"),
            _step("AI önerisini alın", "Get the AI suggestion",
                  "'Öneri Getir' düğmesiyle önerilen miktarları yükleyin.",
                  "Press 'Suggest' to pull the recommended quantities.", None),
            _step("Miktarları düzenleyin", "Adjust the quantities",
                  "Gerekli satırları değiştirin; kapasite göstergesini izleyin.",
                  "Edit lines as needed and watch the capacity indicator.", None),
            _step("Yüklemeyi onaylayın", "Post the load",
                  "Onayladığınızda stok depodan araca geçer.",
                  "Posting moves the stock from the depot to the van.", None),
            _step("Takviye yapın", "Create a top-up",
                  "Gün içinde ihtiyaç olursa 'Takviye' işaretli yeni yükleme açın.",
                  "During the day, open a new load marked as a top-up if needed.", "/stock/van-load"),
        ],
    },
    {
        "code": "07",
        "module": "field",
        "target_route": "/field/routes",
        "estimated_minutes": 10,
        "title_tr": "Rota Planlama ve Optimizasyon",
        "title_en": "Route Planning & Optimisation",
        "summary_tr": "Rota şablonları, günlük rota üretimi, sıralama optimizasyonu ve harita takibi.",
        "summary_en": "Route templates, daily route generation, sequence optimisation and map tracking.",
        "body_tr": (
            "Rota şablonu, bir plasiyerin haftanın belirli gününde ziyaret edeceği müşteri listesidir. "
            "Günlük rotalar bu şablonlardan üretilir; üretilen rota o günün çalışma planıdır.\n\n"
            "Optimizasyon, durakların sırasını mesafe, servis süresi, müşterinin açılış-kapanış saatleri "
            "ve araç kapasitesine göre yeniden düzenler. Amaç toplam yolu kısaltmak değil, günü zamanında "
            "bitirmektir; bu yüzden öncelikli müşteriler sıranın başına alınır.\n\n"
            "Optimizasyon sonucu bir öneridir: plasiyer veya şef sırayı elle değiştirebilir. Değişiklik "
            "kaydedilir ve rota performans raporunda plan/gerçekleşme karşılaştırması yapılır.\n\n"
            "Harita ekranında araçların anlık konumu, tamamlanan ve bekleyen duraklar renklerle gösterilir. "
            "Bir durakta beklenenden uzun kalınırsa gecikme uyarısı üretilir."
        ),
        "body_en": (
            "A route template is the list of customers a salesperson visits on a given weekday. Daily "
            "routes are generated from those templates; the generated route is that day's work plan.\n\n"
            "Optimisation re-sequences the stops using distance, service time, customer opening hours "
            "and vehicle capacity. The goal is not the shortest path but finishing the day on time, so "
            "priority customers are pulled to the front.\n\n"
            "The optimised order is a proposal: the salesperson or supervisor may reorder manually. The "
            "change is recorded, and the route performance report compares plan against actual.\n\n"
            "The map screen shows live vehicle positions with completed and pending stops colour-coded. "
            "Staying longer than expected at a stop raises a delay alert."
        ),
        "steps": [
            _step("Şablon oluşturun", "Create a template",
                  "Haftanın gününü, plasiyeri ve müşterileri seçin.",
                  "Pick the weekday, the salesperson and the customers.", "/field/routes/templates"),
            _step("Günlük rotayı üretin", "Generate the daily route",
                  "Tarih seçip 'Rota Üret' ile o günün rotasını oluşturun.",
                  "Choose a date and press 'Generate' to create the day's route.", "/field/routes"),
            _step("Optimize edin", "Optimise",
                  "'Optimize Et' düğmesiyle durak sırasını yeniden hesaplayın.",
                  "Press 'Optimise' to recompute the stop order.", None),
            _step("Sırayı gözden geçirin", "Review the sequence",
                  "Gerekirse durakları sürükleyerek elle düzenleyin.",
                  "Drag stops to adjust the order manually if needed.", None),
            _step("Haritadan takip edin", "Track on the map",
                  "Gün içinde harita ekranından ilerlemeyi izleyin.",
                  "Follow progress on the map during the day.", "/field/map"),
        ],
    },
    {
        "code": "08",
        "module": "sales",
        "target_route": "/sales/hot-sale",
        "estimated_minutes": 12,
        "title_tr": "Sıcak Satış (Saha Satışı)",
        "title_en": "Hot Sale (Direct Store Delivery)",
        "summary_tr": "Müşteride sipariş, kampanya uygulaması, stok kontrolü, fatura ve teslim.",
        "summary_en": "Ordering at the customer, campaigns, stock checks, invoicing and delivery.",
        "body_tr": (
            "Sıcak satış, malın müşteride anında teslim edildiği satıştır: sipariş, sevk ve fatura tek "
            "işlemde oluşur. Ekran, aracın o anki stoğunu gösterir — satamayacağınız bir ürünü "
            "seçemezsiniz.\n\n"
            "Satır eklerken barkod okutabilir veya arama yapabilirsiniz. Miktarı koli veya adet olarak "
            "girebilirsiniz; sistem temel birime çevirir. Kampanyalar otomatik uygulanır: '10 al 1 "
            "bedava' gibi koşullar sağlandığında bedelsiz satır kendiliğinden eklenir ve satırda "
            "kampanya adı görünür.\n\n"
            "İskonto yetkiniz ürün ve rolünüzle sınırlıdır. Sınırın üstünde iskonto girerseniz sistem "
            "uyarır ve yetkili onayı ister.\n\n"
            "Ödeme nakit, kart, havale, çek/senet veya açık hesap olabilir. Açık hesapta kredi limiti "
            "kontrol edilir. Satış onaylandığında araç stoğu düşer, cari hesap borçlanır, fatura ve "
            "irsaliye numarası üretilir. Müşteri imzası ve teslim fotoğrafı ekleyebilirsiniz."
        ),
        "body_en": (
            "A hot sale delivers goods to the customer immediately: order, delivery and invoice happen "
            "in one transaction. The screen shows the van's current stock — you cannot pick something "
            "you do not have.\n\n"
            "Add lines by scanning a barcode or searching. Enter quantities in cases or pieces; the "
            "system converts to the base unit. Campaigns apply automatically: when a condition such as "
            "'buy 10, get 1 free' is met, the free line is added and the campaign name is shown.\n\n"
            "Your discount authority is limited by the product and your role. Entering more than your "
            "limit raises a warning and requires an authorised approval.\n\n"
            "Payment can be cash, card, transfer, cheque/note or open account. On open account the "
            "credit limit is checked. When the sale is posted, van stock decreases, the customer account "
            "is debited and invoice/waybill numbers are issued. You can capture a signature and a "
            "delivery photo."
        ),
        "steps": [
            _step("Müşteriyi seçin", "Select the customer",
                  "Rotadaki duraktan veya arama ile müşteriyi açın.",
                  "Open the customer from the route stop or by searching.", "/sales/hot-sale"),
            _step("Ürünleri ekleyin", "Add the products",
                  "Barkod okutun veya arayın; miktarı koli/adet olarak girin.",
                  "Scan or search, then enter the quantity in cases or pieces.", None),
            _step("Kampanyayı kontrol edin", "Check the campaign",
                  "Uygulanan kampanyaları ve bedelsiz satırları doğrulayın.",
                  "Verify the applied campaigns and any free-goods lines.", None),
            _step("Ödemeyi girin", "Enter the payment",
                  "Ödeme yöntemini seçin; açık hesapta limit kontrolünü görün.",
                  "Choose the payment method; on open account watch the limit check.", None),
            _step("Satışı tamamlayın", "Complete the sale",
                  "İmza ve fotoğrafı ekleyip satışı onaylayın; fatura üretilir.",
                  "Capture the signature and photo, then post the sale to issue the invoice.", None),
        ],
    },
    {
        "code": "09",
        "module": "finance",
        "target_route": "/sales/payments",
        "estimated_minutes": 9,
        "title_tr": "Tahsilat ve Cari Hesap",
        "title_en": "Collections & Current Account",
        "summary_tr": "Tahsilat kaydı, faturaya dağıtım, çek/senet takibi ve cari ekstre okuma.",
        "summary_en": "Recording collections, allocating to invoices, cheque/note tracking and statements.",
        "body_tr": (
            "Tahsilat, müşteriden alınan paranın kaydıdır ve açık faturalara dağıtılır. Varsayılan "
            "dağıtım en eski faturadan başlar; gerekirse elle değiştirebilirsiniz.\n\n"
            "Çek ve senette vade, banka ve keşideci bilgileri zorunludur; bu belgeler vade tarihine kadar "
            "'beklemede' durumundadır. Karşılıksız çıkan bir çek 'karşılıksız' olarak işaretlenir, "
            "müşterinin bakiyesi ve risk skoru buna göre güncellenir.\n\n"
            "Cari hesap ekstresi borç, alacak ve yürüyen bakiyeyi tarih sırasıyla gösterir. Yaşlandırma "
            "raporu ise açık tutarları vade gruplarına (1-30, 31-60, 61-90, 90+ gün) ayırır; tahsilat "
            "önceliğinizi buna göre belirleyin.\n\n"
            "Sahada alınan nakit, gün sonunda beyan edilen kasa ile karşılaştırılır. Fark varsa gün "
            "kapanışı 'ihtilaflı' olarak işaretlenir ve şefin onayını bekler."
        ),
        "body_en": (
            "A collection records money received from a customer and is allocated across open invoices. "
            "The default allocation starts with the oldest invoice; you can override it.\n\n"
            "For cheques and notes, the maturity date, bank and drawer are mandatory; these instruments "
            "stay 'pending' until maturity. A bounced cheque is marked as such, and the customer's "
            "balance and risk score are updated accordingly.\n\n"
            "The account statement shows debit, credit and running balance in date order. The ageing "
            "report splits open amounts into buckets (1-30, 31-60, 61-90, 90+ days); use it to set your "
            "collection priorities.\n\n"
            "Cash taken in the field is compared with the declared cash at day end. A difference marks "
            "the day session as disputed and requires supervisor approval."
        ),
        "steps": [
            _step("Tahsilat açın", "Open a collection",
                  "Müşteriyi seçip tutarı ve ödeme yöntemini girin.",
                  "Select the customer, then enter the amount and payment method.",
                  "/sales/payments/new"),
            _step("Faturalara dağıtın", "Allocate to invoices",
                  "Açık faturalar listesinden dağıtımı onaylayın veya değiştirin.",
                  "Confirm or adjust the allocation across open invoices.", None),
            _step("Çek/senet bilgisi girin", "Enter cheque details",
                  "Vade, banka ve keşideci alanlarını doldurun.",
                  "Fill in maturity, bank and drawer.", None),
            _step("Ekstreyi okuyun", "Read the statement",
                  "Müşterinin cari ekstresinde yürüyen bakiyeyi inceleyin.",
                  "Review the running balance on the customer statement.", "/crm/ledger"),
            _step("Yaşlandırmayı takip edin", "Follow the ageing",
                  "Alacak yaşlandırma raporundan riskli müşterileri belirleyin.",
                  "Identify risky customers from the receivable ageing report.", "/reports"),
        ],
    },
    {
        "code": "10",
        "module": "sales",
        "target_route": "/sales/returns",
        "estimated_minutes": 8,
        "title_tr": "İade ve Fire Yönetimi",
        "title_en": "Returns & Wastage",
        "summary_tr": "İade nedenleri, satılabilir/imha kararı, iade faturası ve fire kaydı.",
        "summary_en": "Return reasons, resaleable/scrap decisions, credit notes and wastage records.",
        "body_tr": (
            "İade, müşteriden geri alınan maldır ve her zaman bir neden taşır: SKT geçmiş, hasarlı, "
            "yanlış ürün, kalite, fazla stok veya müşteri talebi. Neden, sonraki kararı belirler.\n\n"
            "Karar üç seçeneklidir: 'satılabilir' mal araç veya depo stoğuna geri girer; 'imha' mal fire "
            "olarak düşülür; 'karantina' mal incelenmek üzere ayrı tutulur. SKT geçmiş ve hasarlı mal "
            "asla satılabilir stoğa dönmez.\n\n"
            "İade karşılığında iade faturası (alacak dekontu) üretilir ve müşterinin cari hesabı alacaklanır. "
            "Fotoğraf eklemek, özellikle hasar ve kalite iadelerinde, sonraki itirazları önler.\n\n"
            "Fire, iade dışında da oluşabilir: araçta kırılan bir şişe, depoda SKT'si geçen bir parti. "
            "Fire kaydı stoktan düşer ve fire raporunda ürün bazında izlenir. Yüksek fire, yükleme "
            "planlamasında veya taşıma koşullarında bir sorunun işaretidir."
        ),
        "body_en": (
            "A return is goods taken back from the customer and always carries a reason: expired, "
            "damaged, wrong product, quality, overstock or customer request. The reason drives the "
            "decision that follows.\n\n"
            "There are three dispositions: 'resaleable' goes back into van or depot stock; 'scrap' is "
            "written off as wastage; 'quarantine' is held aside for inspection. Expired and damaged "
            "goods never return to sellable stock.\n\n"
            "A credit note is issued for the return and the customer's account is credited. Attaching a "
            "photo — especially for damage and quality returns — prevents later disputes.\n\n"
            "Wastage also happens outside returns: a bottle broken on the van, a lot expiring in the "
            "depot. Wastage reduces stock and is tracked per product in the wastage report. High wastage "
            "signals a problem in load planning or transport conditions."
        ),
        "steps": [
            _step("İade belgesi açın", "Open a return",
                  "Müşteriyi ve varsa ilgili satışı seçin.",
                  "Select the customer and the related sale if there is one.", "/sales/returns/new"),
            _step("Nedeni seçin", "Choose the reason",
                  "Her satır için iade nedenini belirtin.",
                  "State the return reason for each line.", None),
            _step("Kararı verin", "Decide the disposition",
                  "Satılabilir, imha veya karantina seçeneğini işaretleyin.",
                  "Mark resaleable, scrap or quarantine.", None),
            _step("Fotoğraf ekleyin", "Attach a photo",
                  "Hasar ve kalite iadelerinde fotoğraf zorunlu tutulmalıdır.",
                  "Photos should be mandatory for damage and quality returns.", None),
            _step("Fireyi kaydedin", "Record wastage",
                  "İade dışı kayıpları stok düzeltme ekranından fire olarak girin.",
                  "Enter non-return losses as wastage on the stock adjustment screen.",
                  "/stock/adjustments"),
        ],
    },
    {
        "code": "11",
        "module": "field",
        "target_route": "/field/day-session",
        "estimated_minutes": 10,
        "title_tr": "Gün Sonu Mutabakatı",
        "title_en": "End-of-Day Reconciliation",
        "summary_tr": "Gün açma/kapama, araç sayımı, teorik-fiziki fark ve kasa mutabakatı.",
        "summary_en": "Opening/closing the day, van count, theoretical vs physical variance and cash.",
        "body_tr": (
            "Her plasiyerin günü bir 'gün oturumu' ile açılır ve kapanır. Gün açılmadan satış yapılamaz; "
            "bu, her hareketin bir güne ve bir araca bağlanmasını garanti eder.\n\n"
            "Gün sonunda araç sayımı yapılır ve sistem şu denklemi kurar:\n"
            "  sabah yükleme + takviye − satış − iade − fire = teorik stok\n"
            "  teorik stok − sayılan stok = fark\n\n"
            "Fark sıfırsa gün temiz kapanır. Fark varsa gün 'ihtilaflı' olur; sistem hangi üründe ne kadar "
            "fark olduğunu satır satır gösterir. Küçük farklar için tolerans yüzdesi ayarlardan tanımlanır.\n\n"
            "Kasa mutabakatı ayrı yapılır: gün içinde alınan nakit ile beyan edilen nakit karşılaştırılır. "
            "Hem stok hem kasa farkı, şef onayı olmadan kapanmaz ve denetim kaydına yazılır. Gün kapandıktan "
            "sonra o güne satış girilemez."
        ),
        "body_en": (
            "Each salesperson's day is opened and closed with a 'day session'. No sale is possible "
            "before the day is opened; this guarantees every movement is tied to a day and a vehicle.\n\n"
            "At day end the van is counted and the system evaluates:\n"
            "  morning load + top-ups − sales − returns − wastage = theoretical stock\n"
            "  theoretical stock − counted stock = variance\n\n"
            "Zero variance closes the day cleanly. Otherwise the day becomes disputed and the system "
            "shows the variance product by product. A tolerance percentage for small differences is "
            "configurable in settings.\n\n"
            "Cash is reconciled separately: cash collected during the day is compared with the declared "
            "cash. Both stock and cash differences require supervisor approval to close and are written "
            "to the audit log. Once the day is closed, no sale can be entered for that date."
        ),
        "steps": [
            _step("Günü açın", "Open the day",
                  "Aracı ve başlangıç kilometresini girerek günü başlatın.",
                  "Start the day by entering the vehicle and starting odometer.",
                  "/field/day-session"),
            _step("Gün içinde çalışın", "Work through the day",
                  "Satış, tahsilat ve iadeler otomatik olarak güne bağlanır.",
                  "Sales, collections and returns attach to the day automatically.", None),
            _step("Araç sayımı yapın", "Count the van",
                  "Kalan ürünleri sayıp miktarları girin.",
                  "Count the remaining products and enter the quantities.", None),
            _step("Farkı inceleyin", "Review the variance",
                  "Teorik ve sayılan miktar farkını satır satır kontrol edin.",
                  "Check the theoretical versus counted difference line by line.", None),
            _step("Kasayı beyan edin ve kapatın", "Declare cash and close",
                  "Nakit tutarını beyan edip günü kapatın; fark varsa şef onayı gerekir.",
                  "Declare the cash and close the day; differences need supervisor approval.", None),
        ],
    },
    {
        "code": "12",
        "module": "marketing",
        "target_route": "/marketing/campaigns",
        "estimated_minutes": 9,
        "title_tr": "Kampanya Yönetimi",
        "title_en": "Campaign Management",
        "summary_tr": "Kampanya tipleri, koşullar, hedefleme, bütçe ve performans ölçümü.",
        "summary_en": "Campaign types, conditions, targeting, budget and performance measurement.",
        "body_tr": (
            "Kampanya, koşul ve ödülden oluşur. Koşul 'ne alınırsa' (10 koli X ürünü, 20.000 TL üzeri "
            "sepet, 3 farklı ürün), ödül ise 'ne verilir' (bedava ürün, yüzde iskonto, tutar iskontosu, "
            "sabit fiyat) tarafıdır.\n\n"
            "Hedefleme, kampanyanın kimde geçerli olduğunu belirler: tüm müşteriler, belirli bir kanal, "
            "bölge, rota, müşteri tipi veya tek tek seçilmiş müşteriler. Ürün tarafında da marka veya "
            "kategori bazlı hedefleme yapılabilir.\n\n"
            "Öncelik ve birleşebilirlik önemlidir: aynı sepete uyan iki kampanya varsa önceliği düşük "
            "sayı olan önce uygulanır; 'birleşebilir' değilse ikincisi uygulanmaz.\n\n"
            "Bütçe ve kullanım limitleri kampanyanın kontrolden çıkmasını engeller. Kampanya performans "
            "raporu, verilen iskonto ve bedelsiz mal maliyetine karşılık oluşan sepet tutarını gösterir — "
            "bir kampanyanın gerçekten kâr getirip getirmediğini buradan görürsünüz."
        ),
        "body_en": (
            "A campaign is a condition plus a reward. The condition is 'what must be bought' (10 cases "
            "of product X, a basket over 20,000 TRY, three distinct products) and the reward is 'what is "
            "given' (free goods, percentage discount, amount discount, fixed price).\n\n"
            "Targeting decides who it applies to: all customers, a channel, region, route, customer type "
            "or an explicit list. On the product side you can also target a brand or a category.\n\n"
            "Priority and stackability matter: when two campaigns match the same basket, the lower "
            "priority number applies first, and if it is not stackable the second one does not apply.\n\n"
            "Budgets and usage limits stop a campaign running away. The campaign performance report "
            "compares the discount and free-goods cost given against the basket value generated — this "
            "is where you see whether a promotion actually paid for itself."
        ),
        "steps": [
            _step("Kampanya oluşturun", "Create the campaign",
                  "Tip, tarih aralığı ve adı girin.",
                  "Enter the type, date range and name.", "/marketing/campaigns/new"),
            _step("Koşulları tanımlayın", "Define the conditions",
                  "Minimum miktar, tutar veya ürün çeşidi koşullarını ekleyin.",
                  "Add minimum quantity, amount or product-variety conditions.", None),
            _step("Hedefleyin", "Set the targeting",
                  "Kanal, bölge, müşteri tipi veya ürün grubunu seçin.",
                  "Choose the channel, region, customer type or product group.", None),
            _step("Bütçe ve limit koyun", "Set budget and limits",
                  "Toplam bütçe ve müşteri başına kullanım limitini girin.",
                  "Enter the total budget and per-customer usage limit.", None),
            _step("Performansı ölçün", "Measure the performance",
                  "Kampanya performans raporundan ROI'yi izleyin.",
                  "Track ROI from the campaign performance report.", "/reports"),
        ],
    },
    {
        "code": "13",
        "module": "analytics",
        "target_route": "/reports",
        "estimated_minutes": 8,
        "title_tr": "Raporlar ve Analitik",
        "title_en": "Reports & Analytics",
        "summary_tr": "Rapor çalıştırma, filtreleme, Excel/PDF/CSV dışa aktarma ve hedef takibi.",
        "summary_en": "Running reports, filtering, exporting to Excel/PDF/CSV and tracking targets.",
        "body_tr": (
            "Raporlar ekranı, satış, plasiyer, müşteri, ürün, marka, kategori, bölge, rota, tahsilat, "
            "alacak yaşlandırma, depo/araç stoğu, SKT, fire, iade, kampanya, kârlılık ve hedef "
            "gerçekleşme raporlarını tek yerden çalıştırır.\n\n"
            "Her raporun kendi filtreleri vardır: tarih aralığı zorunlu, diğerleri isteğe bağlıdır. "
            "Rapor sonucu her zaman yetkinizle sınırlıdır — kendi verinizi görürsünüz.\n\n"
            "Sonucu üç biçimde dışa aktarabilirsiniz: Excel (biçimli, toplam satırlı), PDF (yatay A4, "
            "başlık ve sayfa numaralı) ve CSV (Excel'in Türkçe karakterleri doğru açması için BOM'lu "
            "UTF-8). Her dışa aktarma denetim kaydına yazılır.\n\n"
            "Hedef gerçekleşme raporu, dönemin ne kadarının geçtiğine bakarak 'beklenen' yüzdeyi hesaplar "
            "ve gerçekleşmeyi bununla karşılaştırır; 'geride' işaretli satırlar müdahale gerektirir."
        ),
        "body_en": (
            "The reports screen runs sales, salesperson, customer, product, brand, category, region, "
            "route, collections, receivable ageing, warehouse/van stock, expiry, wastage, returns, "
            "campaign, profitability and target-achievement reports from one place.\n\n"
            "Each report has its own filters: the date range is required, the rest optional. Results are "
            "always limited by your permissions — you see your own data.\n\n"
            "Results export in three formats: Excel (styled, with a totals row), PDF (landscape A4 with "
            "header and page numbers) and CSV (UTF-8 with BOM so Excel opens Turkish characters "
            "correctly). Every export is written to the audit log.\n\n"
            "The target-achievement report computes an 'expected' percentage from how much of the period "
            "has elapsed and compares actual against it; rows marked 'behind' need intervention."
        ),
        "steps": [
            _step("Raporu seçin", "Pick the report",
                  "Sol listeden çalıştırmak istediğiniz raporu seçin.",
                  "Choose the report you want from the list on the left.", "/reports"),
            _step("Filtreleri girin", "Set the filters",
                  "Tarih aralığını ve varsa plasiyer/bölge filtrelerini belirleyin.",
                  "Set the date range and any salesperson/region filters.", None),
            _step("Çalıştırın", "Run it",
                  "'Çalıştır' düğmesine basın; sonuç tablosu toplam satırıyla gelir.",
                  "Press 'Run'; the table arrives with a totals row.", None),
            _step("Dışa aktarın", "Export",
                  "Excel, PDF veya CSV biçimlerinden birini seçip indirin.",
                  "Choose Excel, PDF or CSV and download.", None),
            _step("Hedefleri izleyin", "Track targets",
                  "Hedef gerçekleşme raporunda 'geride' satırlarına odaklanın.",
                  "Focus on the 'behind' rows in the target achievement report.", "/analytics/targets"),
        ],
    },
    {
        "code": "14",
        "module": "system",
        "target_route": "/system",
        "estimated_minutes": 12,
        "title_tr": "Sistem Yönetimi",
        "title_en": "System Administration",
        "summary_tr": "Kullanıcı ve rol yönetimi, ayarlar, yedekleme/geri yükleme, denetim kaydı ve sağlık.",
        "summary_en": "Users and roles, settings, backup/restore, audit log and system health.",
        "required_role": RoleCode.SYSTEM_ADMIN,
        "body_tr": (
            "Sistem yönetimi ekranları yalnızca yetkili rollere açıktır. Kullanıcı açarken rol seçimi "
            "yetkiyi belirler; kendinizden üst bir rol atayamazsınız. Gerekirse tek tek izin ekleyip "
            "çıkarabilirsiniz, ancak sahip olmadığınız bir izni veremezsiniz.\n\n"
            "Ayarlar ekranı KDV oranı, SKT uyarı süresi, sayım toleransı, rota hız katsayısı, AI bütçesi "
            "ve yedekleme sıklığı gibi iş kurallarını değiştirir. Gizli işaretli ayarlar (API anahtarları) "
            "ekranda maskelenir ve denetim kaydına açık yazılmaz.\n\n"
            "Yedekleme, veritabanının tutarlı bir kopyasını alır, sıkıştırır ve SHA-256 özetini saklar. "
            "Doğrulama işlemi özeti yeniden hesaplar ve arşivin içindeki veritabanını bütünlük kontrolünden "
            "geçirir. Geri yükleme, işlemden önce mutlaka otomatik bir güvenlik yedeği alır ve onay "
            "kutusu işaretlenmeden çalışmaz.\n\n"
            "Denetim kaydı zincirlidir: her satırın özeti bir öncekine bağlıdır. 'Zincir Doğrula' "
            "düğmesi kayıtların değiştirilmediğini kanıtlar. Sistem sağlığı ekranı veritabanı, disk, "
            "yedek ve AI sağlayıcılarının durumunu tek bakışta gösterir."
        ),
        "body_en": (
            "System administration screens are restricted to authorised roles. When creating a user, the "
            "role decides their authority; you cannot assign a role higher than your own. You may grant "
            "or revoke individual permissions, but never one you do not hold yourself.\n\n"
            "The settings screen changes business rules such as VAT rate, expiry warning window, count "
            "tolerance, route speed factor, AI budget and backup frequency. Settings marked secret (API "
            "keys) are masked on screen and never written to the audit log in clear.\n\n"
            "Backup takes a consistent copy of the database, compresses it and stores a SHA-256 digest. "
            "Verification recomputes the digest and runs an integrity check on the database inside the "
            "archive. Restore always takes an automatic safety backup first and refuses to run without "
            "the confirmation checkbox.\n\n"
            "The audit log is chained: each row's digest depends on the previous one. 'Verify chain' "
            "proves no record has been altered. The system health screen shows database, disk, backup "
            "and AI provider status at a glance."
        ),
        "steps": [
            _step("Kullanıcı açın", "Create a user",
                  "Kullanıcı adı, ad-soyad ve rol seçerek hesap oluşturun.",
                  "Create the account with username, full name and role.", "/system/users/new"),
            _step("İzinleri düzenleyin", "Adjust permissions",
                  "Gerekirse kullanıcıya özel izin ekleyin veya kaldırın.",
                  "Add or remove per-user permission overrides if needed.", "/system/users"),
            _step("Ayarları güncelleyin", "Update the settings",
                  "İş kurallarını ayarlar ekranından değiştirip kaydedin.",
                  "Change and save business rules on the settings screen.", "/system/settings"),
            _step("Yedek alın ve doğrulayın", "Back up and verify",
                  "Yedek oluşturun, ardından 'Doğrula' ile bütünlüğünü kontrol edin.",
                  "Create a backup, then press 'Verify' to check its integrity.", "/system/backup"),
            _step("Denetim zincirini doğrulayın", "Verify the audit chain",
                  "Denetim kaydı ekranından zincir bütünlüğünü kontrol edin.",
                  "Check chain integrity from the audit log screen.", "/system/audit"),
            _step("Sistem sağlığını izleyin", "Watch system health",
                  "Veritabanı, disk, yedek ve AI durumlarını düzenli kontrol edin.",
                  "Regularly check database, disk, backup and AI status.", "/system/health"),
        ],
    },
]


# ===========================================================================
# Seeding
# ===========================================================================
def seed_lessons(db: Session, *, refresh_content: bool = True) -> int:
    """
    Create (or refresh) the 14 lessons.

    Idempotent: existing lessons keep their id — and therefore everyone's
    progress — while their text is brought up to date.
    """
    existing = {
        row.code: row for row in db.execute(select(TrainingLesson)).scalars()
    }
    created = 0
    for order, spec in enumerate(LESSONS, start=1):
        row = existing.get(spec["code"])
        payload = {
            "sort_order": order,
            "module": spec.get("module"),
            "title_tr": spec["title_tr"],
            "title_en": spec["title_en"],
            "summary_tr": spec.get("summary_tr"),
            "summary_en": spec.get("summary_en"),
            "body_tr": spec["body_tr"],
            "body_en": spec["body_en"],
            "steps": dumps(spec.get("steps") or []),
            "target_route": spec.get("target_route"),
            "estimated_minutes": spec.get("estimated_minutes", 5),
            "required_role": spec.get("required_role"),
            "is_published": True,
        }
        if row is None:
            db.add(TrainingLesson(code=spec["code"], **payload))
            created += 1
        elif refresh_content:
            for key, value in payload.items():
                setattr(row, key, value)
    db.commit()
    if created:
        log.info("Seeded %d training lesson(s)", created)
    return created


# ===========================================================================
# Reads
# ===========================================================================
def _visible(lesson: TrainingLesson, user: Any) -> bool:
    """A lesson restricted to a role is hidden from everyone else."""
    if not lesson.is_published:
        return False
    if not lesson.required_role:
        return True
    if user is None:
        return False
    if auth_service.is_admin(user):
        return True
    return getattr(getattr(user, "role", None), "code", None) == lesson.required_role


def as_dict(
    lesson: TrainingLesson,
    lang: str = "tr",
    *,
    progress: TrainingProgress | None = None,
    include_body: bool = True,
) -> dict[str, Any]:
    steps_raw = loads(lesson.steps, []) or []
    steps = [
        {
            "index": idx,
            "title": (s.get("title_en") if lang == "en" else s.get("title_tr")) or "",
            "detail": (s.get("detail_en") if lang == "en" else s.get("detail_tr")) or "",
            "title_tr": s.get("title_tr"),
            "title_en": s.get("title_en"),
            "detail_tr": s.get("detail_tr"),
            "detail_en": s.get("detail_en"),
            "screen": s.get("screen"),
        }
        for idx, s in enumerate(steps_raw, start=1)
    ]
    out: dict[str, Any] = {
        "id": lesson.id,
        "code": lesson.code,
        "module": lesson.module,
        "sort_order": lesson.sort_order,
        "title": lesson.title_en if lang == "en" else lesson.title_tr,
        "title_tr": lesson.title_tr,
        "title_en": lesson.title_en,
        "summary": (lesson.summary_en if lang == "en" else lesson.summary_tr) or "",
        "target_route": lesson.target_route,
        "estimated_minutes": lesson.estimated_minutes,
        "required_role": lesson.required_role,
        "step_count": len(steps),
        "steps": steps,
        "is_completed": bool(progress and progress.is_completed),
        "progress_percent": float(progress.progress_percent) if progress else 0.0,
        "last_step": int(progress.last_step) if progress else 0,
        "completed_at": progress.completed_at if progress else None,
    }
    if include_body:
        out["body"] = lesson.body_en if lang == "en" else lesson.body_tr
        out["body_tr"] = lesson.body_tr
        out["body_en"] = lesson.body_en
    return out


def list_lessons(
    db: Session, *, user: Any = None, lang: str = "tr", module: str | None = None
) -> list[dict[str, Any]]:
    """Every lesson the user may see, with their own progress merged in."""
    conds: list[Any] = [TrainingLesson.is_published.is_(True)]
    if module:
        conds.append(TrainingLesson.module == module)

    lessons = db.execute(
        select(TrainingLesson).where(*conds).order_by(TrainingLesson.sort_order)
    ).scalars().all()

    progress_by_lesson: dict[int, TrainingProgress] = {}
    user_id = getattr(user, "id", None)
    if user_id:
        progress_by_lesson = {
            p.lesson_id: p
            for p in db.execute(
                select(TrainingProgress).where(TrainingProgress.user_id == user_id)
            ).scalars()
        }

    return [
        as_dict(lesson, lang, progress=progress_by_lesson.get(lesson.id), include_body=False)
        for lesson in lessons
        if _visible(lesson, user)
    ]


def get_lesson(
    db: Session, identifier: int | str, *, user: Any = None, lang: str = "tr"
) -> dict[str, Any]:
    """Fetch a lesson by numeric id or by its two-digit code."""
    lesson: TrainingLesson | None
    if isinstance(identifier, int):
        lesson = db.get(TrainingLesson, identifier)
    else:
        text_id = str(identifier).strip()
        lesson = db.execute(
            select(TrainingLesson).where(TrainingLesson.code == text_id)
        ).scalar_one_or_none()
        if lesson is None and text_id.isdigit():
            lesson = db.get(TrainingLesson, int(text_id))

    if lesson is None or not _visible(lesson, user):
        raise NotFoundError("training.lesson_not_found", params={"id": identifier})

    progress = None
    user_id = getattr(user, "id", None)
    if user_id:
        progress = db.execute(
            select(TrainingProgress).where(
                TrainingProgress.user_id == user_id, TrainingProgress.lesson_id == lesson.id
            )
        ).scalar_one_or_none()
    return as_dict(lesson, lang, progress=progress)


# ===========================================================================
# Progress
# ===========================================================================
def mark_progress(
    db: Session,
    *,
    user_id: int,
    lesson_id: int,
    last_step: int | None = None,
    progress_percent: float | None = None,
    is_completed: bool | None = None,
    score: float | None = None,
) -> TrainingProgress:
    """Record how far a user has got; completion also forces 100%."""
    lesson = db.get(TrainingLesson, lesson_id)
    if lesson is None:
        raise NotFoundError("training.lesson_not_found", params={"id": lesson_id})
    if not user_id:
        raise ValidationError("training.user_required")

    row = db.execute(
        select(TrainingProgress).where(
            TrainingProgress.user_id == user_id, TrainingProgress.lesson_id == lesson_id
        )
    ).scalar_one_or_none()
    if row is None:
        row = TrainingProgress(user_id=user_id, lesson_id=lesson_id)
        db.add(row)

    steps = loads(lesson.steps, []) or []
    if last_step is not None:
        row.last_step = int(clamp(float(last_step), 0, float(max(1, len(steps)))))
        if progress_percent is None and steps:
            progress_percent = row.last_step / len(steps) * 100
    if progress_percent is not None:
        row.progress_percent = clamp(float(progress_percent), 0.0, 100.0)
    if score is not None:
        row.score = clamp(float(score), 0.0, 100.0)
    if is_completed:
        row.is_completed = True
        row.completed_at = utcnow()
        row.progress_percent = 100.0
        row.last_step = len(steps)
    elif is_completed is False:
        row.is_completed = False
        row.completed_at = None

    db.commit()
    return row


def progress_summary(db: Session, user_id: int) -> dict[str, Any]:
    """Completion overview for one user — drives the training centre header."""
    total = int(
        db.execute(
            select(func.count(TrainingLesson.id)).where(TrainingLesson.is_published.is_(True))
        ).scalar_one()
        or 0
    )
    rows = db.execute(
        select(TrainingProgress).where(TrainingProgress.user_id == user_id)
    ).scalars().all()
    completed = sum(1 for r in rows if r.is_completed)
    started = sum(1 for r in rows if not r.is_completed and r.progress_percent > 0)
    minutes = int(
        db.execute(
            select(func.sum(TrainingLesson.estimated_minutes)).where(
                TrainingLesson.is_published.is_(True)
            )
        ).scalar_one()
        or 0
    )
    return {
        "total_lessons": total,
        "completed_lessons": completed,
        "in_progress_lessons": started,
        "not_started_lessons": max(0, total - completed - started),
        "completion_percent": round(completed / total * 100, 1) if total else 0.0,
        "total_minutes": minutes,
        "last_activity_at": max((r.updated_at for r in rows), default=None),
    }


__all__ = [
    "LESSONS",
    "as_dict",
    "get_lesson",
    "list_lessons",
    "mark_progress",
    "progress_summary",
    "seed_lessons",
]
