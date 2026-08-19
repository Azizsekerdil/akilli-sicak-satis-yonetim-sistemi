"""
Discovery scanner — repoyu ölçer, tahmin etmez.

Faz 0'ın çekirdeği. Bir uyumluluk raporunun değeri, dayandığı envanterin
gerçekten ölçülmüş olmasına bağlıdır; bu yüzden burada hiçbir bulgu elle
yazılmaz. Her satır dosya sisteminden, bağımlılık meta verisinden veya ORM
model tanımlarından okunur.

Ürettiği envanter altı başlıkta toplanır:

    dependencies      pip + npm paketleri, sürüm ve lisansları
    personal_data     ORM alanlarından türetilen kişisel veri envanteri
    ai_providers      yapılandırılmış AI sağlayıcıları ve uç noktaları
    external_calls    kaynak kodda geçen dış HTTP uç noktaları
    automation        insan onayı olmadan çalışan otomatik karar noktaları
    human_control     onay, geçersiz kılma ve itiraz noktaları

Bilinmeyen bir değer asla "uygun" sayılmaz; `UNKNOWN` olarak raporlanır ve
`review_required` listesine düşer.

    python -m app.compliance.scanners.discovery            # JSON
    python -m app.compliance.scanners.discovery --markdown # rapor
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = BACKEND_ROOT.parent

# ---------------------------------------------------------------------------
# Sınıflandırma sözlükleri
# ---------------------------------------------------------------------------
#: Eslesme SOZCUK temellidir, hece temelli DEGIL.
#: Ilk surumde alt dize aramasi kullanildi ve dordu de yanlis alarm cikti:
#: "supports_embeddings", "heading" ve "padding" icindeki "din" hecesi
#: (din = religion) ile "is_healthy" icindeki "health" eslesti. Yanlis alarm
#: ureten bir uyumluluk tarayicisi okunmaz hale gelir; bu yuzden alan adi
#: once TAM IFADE olarak, sonra alt cizgiyle bolunmus SOZCUKLER olarak
#: karsilastirilir.

#: KVKK m.6 / GDPR Art.9 anlaminda ozel nitelikli olabilecek alan adlari.
#: Eslesme *aday* uretir; nihai siniflandirma insan incelemesine birakilir.
OZEL_NITELIKLI_IFADE = {
    "health_data", "health_status", "medical_record", "blood_type",
    "saglik_verisi", "kan_grubu", "biometric_template", "biyometrik_veri",
    "fingerprint_hash", "face_encoding", "genetic_data", "criminal_record",
    "sabika_kaydi", "union_membership", "sendika_uyeligi", "sexual_orientation",
    "religious_belief", "dini_inanc", "political_opinion", "siyasi_gorus",
    "ethnic_origin", "etnik_koken", "disability_status", "engellilik_durumu",
}
OZEL_NITELIKLI_SOZCUK = {
    "health", "saglik", "biometric", "biyometri", "biyometrik", "fingerprint",
    "parmakizi", "genetic", "genetik", "religion", "din", "ethnicity", "etnik",
    "union", "sendika", "criminal", "sabika", "sexual", "cinsel", "belief",
    "philosophical", "political", "siyasi", "disability", "engellilik",
    "diagnosis", "teshis", "medication", "ilac",
}

KISISEL_IFADE = {
    "full_name", "first_name", "last_name", "contact_person", "drawer_name",
    "tax_number", "national_id", "tckn", "vergi_no", "ip_address",
    "user_agent", "postal_code", "posta_kodu", "birth_date", "dogum_tarihi",
    "image_path", "photo_path", "avatar_path", "signature_path",
    "device_label", "last_login_ip", "plate_number", "password_hash",
}
KISISEL_SOZCUK = {
    "soyad", "surname", "fullname", "email", "eposta",
    "phone", "telefon", "mobile", "gsm", "address", "adres", "city", "sehir",
    "district", "ilce", "neighbourhood", "mahalle", "postal", "zip", "iban",
    "birth", "dogum", "age", "yas", "gender", "cinsiyet", "photo", "foto",
    "avatar", "signature", "imza", "username", "password", "salary", "maas",
    "commission", "plate", "plaka",
}

#: Dogrudan kimlik belirleyen alanlar — maskeleme onceligi en yuksek.
DOGRUDAN_IFADE = {
    "tckn", "national_id", "tax_number", "vergi_no", "ip_address",
    "password_hash", "last_login_ip", "plate_number",
}
DOGRUDAN_SOZCUK = {
    "email", "eposta", "phone", "telefon", "mobile", "gsm", "iban", "password",
}

#: Konum verisi ayri ele alinir: izleme baglaminda orantililik degerlendirmesi
#: gerektirir.
KONUM_IFADE = {
    "latitude", "longitude", "center_lat", "center_lng", "last_lat",
    "last_lng", "arrival_lat", "arrival_lng", "accuracy_m",
    "geofence_distance_m",
}
KONUM_SOZCUK = {"latitude", "longitude", "gps", "koordinat"}

#: "name" gibi genel token'lar YALNIZCA kisi tasiyan tablolarda kisisel veridir.
#: products.name bir urun adidir, roles.name bir rol adidir; bunlari kisisel
#: veri saymak envanteri sisirir ve gercek bulgulari gurultuye gomer.
KISI_TABLOLARI = {
    "users", "customers", "customer_contacts", "salespersons", "companies",
    "branches", "user_sessions", "login_attempts", "audit_logs", "visits",
    "customer_notes",
}
BAGLAM_GEREKTIREN_SOZCUK = {"name", "ad", "isim", "title", "unvan"}

#: Parolayla ilgili META alanlari kimlik belirlemez.
PAROLA_META = {"password_changed_at", "must_change_password",
               "password_min_length", "password_expires_at"}

URL_DESENI = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")

#: Taramadan muaf dizinler — üretilmiş veya üçüncü taraf içerik.
ATLA = {
    ".git", ".venv", "node_modules", "__pycache__", "dist", "build",
    ".pytest_cache", ".ruff_cache", "backups", "logs", "data", "htmlcov",
    "docs/sunum", "compliance/patent",
}


def _atlanir(yol: Path) -> bool:
    p = yol.as_posix()
    return any(("/" + a + "/") in ("/" + p + "/") or p.endswith("/" + a)
               for a in ATLA)


# ---------------------------------------------------------------------------
# Veri sınıfları
# ---------------------------------------------------------------------------
@dataclass
class Bagimlilik:
    ad: str
    surum: str
    lisans: str
    ekosistem: str
    dogrudan: bool
    risk: str = "OK"          # OK | REVIEW_REQUIRED | UNKNOWN


@dataclass
class VeriAlani:
    tablo: str
    alan: str
    tip: str
    kategori: str             # KISISEL | OZEL_NITELIKLI | KONUM | DIGER
    tanimlayici: bool
    dosya: str
    satir: int


@dataclass
class DisCagri:
    url: str
    dosya: str
    satir: int
    tur: str                  # AI_PROVIDER | MAP_TILES | RESMI_KAYNAK | DIGER


@dataclass
class OtomasyonNoktasi:
    ad: str
    dosya: str
    satir: int
    aciklama: str
    insan_onayi: str          # VAR | YOK | KISMI | UNKNOWN


@dataclass
class Envanter:
    olusturma_zamani: str
    proje_koku: str
    dependencies: list[Bagimlilik] = field(default_factory=list)
    personal_data: list[VeriAlani] = field(default_factory=list)
    ai_providers: list[dict[str, Any]] = field(default_factory=list)
    external_calls: list[DisCagri] = field(default_factory=list)
    automation: list[OtomasyonNoktasi] = field(default_factory=list)
    human_control: list[OtomasyonNoktasi] = field(default_factory=list)
    review_required: list[str] = field(default_factory=list)
    ozet: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1) Bağımlılıklar
# ---------------------------------------------------------------------------
def _dogrudan_pip() -> set[str]:
    adlar: set[str] = set()
    for ad in ("requirements.txt", "requirements-dev.txt"):
        yol = BACKEND_ROOT / ad
        if not yol.exists():
            continue
        for satir in yol.read_text(encoding="utf-8").splitlines():
            satir = satir.split("#")[0].strip()
            if not satir or satir.startswith("-r"):
                continue
            adlar.add(re.split(r"[<>=\[]", satir)[0].strip().lower())
    return adlar


def tara_bagimliliklar() -> tuple[list[Bagimlilik], list[str]]:
    """Kurulu paketleri ve lisanslarını okur.  Lisansı okunamayan paket
    `UNKNOWN` olarak işaretlenir ve insan incelemesine düşer."""
    from importlib import metadata

    dogrudan = _dogrudan_pip()
    out: list[Bagimlilik] = []
    inceleme: list[str] = []

    for dist in metadata.distributions():
        try:
            meta = dist.metadata
            ad = meta["Name"]
            if not ad:
                continue
            lisans = (meta.get("License-Expression") or meta.get("License") or "").strip()
            if not lisans or len(lisans) > 60:
                siniflar = [c for c in (meta.get_all("Classifier") or [])
                            if c.startswith("License ::")]
                lisans = siniflar[-1].split("::")[-1].strip() if siniflar else ""
            lisans = lisans or "UNKNOWN"
            risk = "OK"
            if lisans == "UNKNOWN":
                risk = "UNKNOWN"
                inceleme.append("Lisansı okunamayan pip paketi: %s" % ad)
            elif re.search(r"\b(GPL|AGPL|LGPL)\b", lisans, re.I):
                risk = "REVIEW_REQUIRED"
                inceleme.append("Copyleft lisanslı pip paketi: %s (%s)" % (ad, lisans))
            out.append(Bagimlilik(ad, dist.version, lisans, "pip",
                                  ad.replace("_", "-").lower() in dogrudan, risk))
        except Exception:
            continue

    # npm — kurulu değilse sessizce atlanır, uydurulmaz.
    pkg = PROJECT_ROOT / "frontend" / "package.json"
    if (PROJECT_ROOT / "frontend" / "node_modules").is_dir() and pkg.exists():
        try:
            # npm'in yolu acikca cozulur ve kabuk KULLANILMAZ. Windows'ta
            # "npm" bir .cmd sarmalayicidir; shell=True bunu bulmanin kolay
            # yoluydu ama komut satirini kabuga teslim eder. Burada saldirgan
            # girdisi yok, yine de kabuksuz calistirmak bu dosyanin ileride
            # degisken bir argumanla genisletilmesi ihtimalini bastan kapatir.
            npm = shutil.which("npm") or shutil.which("npm.cmd")
            if not npm:
                raise FileNotFoundError("npm")
            # encoding acikca verilmeli: Turkce Windows'ta varsayilan cp1254
            # npm'in UTF-8 JSON ciktisini cozemiyor ve tarama cokuyor.
            ham = subprocess.run(
                [npm, "ls", "--all", "--json", "--long"],
                cwd=str(PROJECT_ROOT / "frontend"), capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=240, shell=False)
            veri = json.loads(ham.stdout or "{}")
            paket = json.loads(pkg.read_text(encoding="utf-8"))
            dogrudan_npm = set(paket.get("dependencies", {})) | \
                set(paket.get("devDependencies", {}))
            gorulen: set[str] = set()

            def gez(dugum: dict) -> None:
                for ad, bilgi in (dugum.get("dependencies") or {}).items():
                    if ad not in gorulen:
                        gorulen.add(ad)
                        lisans = str(bilgi.get("license") or "UNKNOWN")
                        surum = str(bilgi.get("version") or "")
                        risk = "OK"
                        if not surum:
                            # Bagimlilik agacinda gorunen ama bu platformda
                            # KURULMAYAN istege bagli paket (ornegin baska bir
                            # islemci mimarisi icin esbuild ikilisi, macOS icin
                            # fsevents). Dagitilan urunde yer almadigi icin
                            # lisans yukumlulugu dogurmaz; "bilinmeyen lisans"
                            # diye raporlamak sayiyi yaniltici sisirir.
                            risk = "NOT_INSTALLED"
                        elif lisans == "UNKNOWN":
                            risk = "UNKNOWN"
                            inceleme.append("Lisansı okunamayan npm paketi: %s" % ad)
                        elif re.search(r"\b(GPL|AGPL)\b", lisans, re.I):
                            risk = "REVIEW_REQUIRED"
                            inceleme.append("Copyleft npm paketi: %s (%s)" % (ad, lisans))
                        elif not re.match(
                                r"^(MIT|ISC|BSD|Apache|PSF|MPL|Unlicense|CC0|Python)",
                                lisans, re.I):
                            # İzin listesi dışındaki her lisans insan onayı ister;
                            # "tanımadığım lisans" sessizce OK sayılmaz.
                            risk = "REVIEW_REQUIRED"
                            inceleme.append(
                                "İzin listesi dışı npm lisansı: %s (%s)" % (ad, lisans))
                        out.append(Bagimlilik(ad, surum, lisans, "npm",
                                              ad in dogrudan_npm, risk))
                    gez(bilgi)

            gez(veri)
        except Exception as exc:
            inceleme.append("npm envanteri alınamadı: %s" % exc)
    else:
        inceleme.append("npm envanteri atlandı (node_modules yok)")

    return out, inceleme


# ---------------------------------------------------------------------------
# 2) Kişisel veri envanteri — ORM modellerinden
# ---------------------------------------------------------------------------
ALAN_DESENI = re.compile(
    r"^\s*(\w+)\s*:\s*Mapped\[[^\]]+\]\s*=\s*(?:mapped_column|fk)\s*\(([^\n]*)")
TABLO_DESENI = re.compile(r'^\s*__tablename__\s*=\s*"([^"]+)"')


def _sozcukler(alan: str) -> set[str]:
    """Alan adini alt cizgi ve buyuk harf sinirlarindan sozcuklere ayirir."""
    ara = re.sub(r"(?<!^)(?=[A-Z])", "_", alan)
    return {p for p in ara.lower().split("_") if p}


def _kategori(alan: str, tablo: str = "") -> tuple[str, bool]:
    """
    (kategori, dogrudan_tanimlayici_mi)

    ``tablo`` verildiginde baglam duyarli calisir: ``name`` alani yalnizca
    kisi tasiyan bir tabloda kisisel veridir.
    """
    a = alan.lower()
    sz = _sozcukler(alan)

    if a in PAROLA_META:
        return "DIGER", False

    dogrudan = a in DOGRUDAN_IFADE or bool(sz & DOGRUDAN_SOZCUK)

    if a in OZEL_NITELIKLI_IFADE or (sz & OZEL_NITELIKLI_SOZCUK):
        return "OZEL_NITELIKLI", dogrudan
    if a in KONUM_IFADE or (sz & KONUM_SOZCUK):
        return "KONUM", False
    if a in KISISEL_IFADE or (sz & KISISEL_SOZCUK):
        return "KISISEL", dogrudan
    if (sz & BAGLAM_GEREKTIREN_SOZCUK) and tablo.lower() in KISI_TABLOLARI:
        return "KISISEL", dogrudan
    return "DIGER", False


def tara_kisisel_veri() -> tuple[list[VeriAlani], list[str]]:
    """ORM model dosyalarını okuyarak kişisel veri alanlarını çıkarır."""
    out: list[VeriAlani] = []
    inceleme: list[str] = []
    model_dizin = BACKEND_ROOT / "app" / "models"
    if not model_dizin.is_dir():
        return out, ["ORM model dizini bulunamadı: %s" % model_dizin]

    for dosya in sorted(model_dizin.glob("*.py")):
        if dosya.name == "__init__.py":
            continue
        tablo = "?"
        for i, satir in enumerate(dosya.read_text(encoding="utf-8").splitlines(), 1):
            m_tablo = TABLO_DESENI.match(satir)
            if m_tablo:
                tablo = m_tablo.group(1)
                continue
            m = ALAN_DESENI.match(satir)
            if not m:
                continue
            alan, govde = m.group(1), m.group(2)
            kategori, tanimlayici = _kategori(alan, tablo)
            if kategori == "DIGER":
                continue
            tip = "String" if "String" in govde else (
                "Float" if "Float" in govde else (
                    "Text" if "Text" in govde else "?"))
            out.append(VeriAlani(tablo, alan, tip, kategori, tanimlayici,
                                 dosya.name, i))

    ozel = [v for v in out if v.kategori == "OZEL_NITELIKLI"]
    if ozel:
        inceleme.append(
            "Özel nitelikli olabilecek %d alan aday olarak işaretlendi; "
            "KVKK m.6 / GDPR Art.9 değerlendirmesi insan onayı ister." % len(ozel))
    konum = [v for v in out if v.kategori == "KONUM"]
    if konum:
        inceleme.append(
            "%d konum alanı bulundu; çalışan izleme bağlamında orantılılık ve "
            "aydınlatma değerlendirmesi gerekir." % len(konum))
    return out, inceleme


# ---------------------------------------------------------------------------
# 3) AI sağlayıcıları
# ---------------------------------------------------------------------------
def tara_ai_saglayicilari() -> tuple[list[dict[str, Any]], list[str]]:
    """Yapılandırmadaki AI sağlayıcılarını okur.  API anahtarının kendisi
    ASLA okunmaz; yalnızca 'tanımlı mı' bilgisi raporlanır."""
    out: list[dict[str, Any]] = []
    inceleme: list[str] = []
    try:
        sys.path.insert(0, str(BACKEND_ROOT))
        from app.core.config import settings

        for ad, aktif, url, model, anahtar_var, yerel in (
            ("LM Studio", settings.lmstudio_enabled, settings.lmstudio_base_url,
             settings.lmstudio_model, True, True),
            ("NVIDIA NIM", settings.nvidia_enabled, settings.nvidia_base_url,
             settings.nvidia_model, bool(settings.nvidia_api_key), False),
            ("Anthropic Claude", settings.claude_enabled, settings.claude_base_url,
             settings.claude_model, bool(settings.claude_api_key), False),
        ):
            kayit = {
                "ad": ad, "aktif": bool(aktif), "base_url": url,
                "varsayilan_model": model,
                "anahtar_tanimli": bool(anahtar_var),   # değerin kendisi değil
                "yerel_mi": yerel,
                "yurt_disi_aktarim": "HAYIR" if yerel else "EVET",
                "transfer_degerlendirmesi": "TAMAM" if yerel else "REVIEW_REQUIRED",
            }
            out.append(kayit)
            if aktif and not yerel:
                inceleme.append(
                    "%s bulut sağlayıcısı aktif — KVKK m.9 / GDPR Art.44-49 "
                    "yurt dışı aktarım değerlendirmesi ve TIA gerekir." % ad)
    except Exception as exc:
        inceleme.append("AI sağlayıcı yapılandırması okunamadı: %s" % exc)
    return out, inceleme


# ---------------------------------------------------------------------------
# 4) Dış çağrılar
# ---------------------------------------------------------------------------
def _url_turu(url: str) -> str:
    u = url.lower()
    if any(k in u for k in ("nvidia.com", "anthropic.com", "localhost:1234",
                            "openai.com")):
        return "AI_PROVIDER"
    if "openstreetmap" in u or "tile." in u:
        return "MAP_TILES"
    if any(k in u for k in ("mevzuat.gov.tr", "kvkk.gov.tr", "eur-lex",
                            "ico.org.uk", "oag.ca.gov", "cppa.ca.gov")):
        return "RESMI_KAYNAK"
    return "DIGER"


def tara_dis_cagrilar() -> tuple[list[DisCagri], list[str]]:
    out: list[DisCagri] = []
    inceleme: list[str] = []
    gorulen: set[str] = set()
    for kok in (BACKEND_ROOT / "app", PROJECT_ROOT / "frontend" / "src"):
        if not kok.is_dir():
            continue
        for dosya in kok.rglob("*"):
            if not dosya.is_file() or _atlanir(dosya.relative_to(PROJECT_ROOT)):
                continue
            if dosya.suffix.lower() not in (".py", ".ts", ".tsx", ".js", ".json"):
                continue
            try:
                icerik = dosya.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, satir in enumerate(icerik.splitlines(), 1):
                for m in URL_DESENI.finditer(satir):
                    url = m.group(0).rstrip(".,);\"'")
                    # Şema/dokümantasyon bağlantıları envanter dışıdır.
                    if any(k in url for k in ("schema.org", "w3.org", "example.com",
                                              "github.com", "sqlalche.me")):
                        continue
                    anahtar = url + "|" + dosya.name
                    if anahtar in gorulen:
                        continue
                    gorulen.add(anahtar)
                    out.append(DisCagri(
                        url, str(dosya.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                        i, _url_turu(url)))
    harita = [c for c in out if c.tur == "MAP_TILES"]
    if harita:
        inceleme.append(
            "Harita karoları dış sunucudan çekiliyor; kullanıcı IP'si üçüncü "
            "tarafa gider. Aydınlatma metnine eklenmeli.")
    return out, inceleme


# ---------------------------------------------------------------------------
# 5) Otomasyon ve insan kontrolü
# ---------------------------------------------------------------------------
#: (desen, açıklama, insan onayı durumu)
OTOMASYON_DESENLERI = [
    (r"def\s+(suggest_van_load|suggest_order)\b",
     "Yapay zeka destekli miktar önerisi", "KISMI"),
    (r"def\s+(risk_score|churn_candidates)\b",
     "Müşteri risk/churn skorlaması — profilleme", "YOK"),
    (r"def\s+(detect_anomalies)\b",
     "Otomatik anomali tespiti ve işaretleme", "YOK"),
    (r"def\s+(check_credit)\b",
     "Kredi limiti kontrolü — satışı otomatik reddedebilir", "YOK"),
    (r"def\s+(forecast_demand|ensemble)\b",
     "Talep tahmini", "KISMI"),
    (r"def\s+(run_readonly|validate)\b.*sql",
     "Doğal dilden üretilen SQL sorgusu", "KISMI"),
]

INSAN_KONTROL_DESENLERI = [
    (r"def\s+approve_count\b", "Sayım farkı insan onayıyla kapanır", "VAR"),
    (r"requires_approval", "AI terminal komutu onay bekler", "VAR"),
    (r"def\s+require_permission\b", "İşlem öncesi yetki denetimi", "VAR"),
    (r"def\s+verify_chain\b", "Denetim kaydı bütünlük doğrulaması", "VAR"),
    (r"is_allowed", "AI terminal izin kapısı", "VAR"),
]


def _desen_tara(desenler, kok: Path) -> list[OtomasyonNoktasi]:
    out: list[OtomasyonNoktasi] = []
    for dosya in kok.rglob("*.py"):
        if _atlanir(dosya.relative_to(PROJECT_ROOT)):
            continue
        try:
            satirlar = dosya.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for i, satir in enumerate(satirlar, 1):
            for desen, aciklama, onay in desenler:
                if re.search(desen, satir, re.I):
                    out.append(OtomasyonNoktasi(
                        satir.strip()[:90],
                        str(dosya.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                        i, aciklama, onay))
                    break
    return out


def tara_otomasyon() -> tuple[list[OtomasyonNoktasi], list[OtomasyonNoktasi], list[str]]:
    kok = BACKEND_ROOT / "app"
    otomasyon = _desen_tara(OTOMASYON_DESENLERI, kok)
    kontrol = _desen_tara(INSAN_KONTROL_DESENLERI, kok)
    inceleme: list[str] = []
    onaysiz = [o for o in otomasyon if o.insan_onayi == "YOK"]
    if onaysiz:
        inceleme.append(
            "%d otomatik karar noktasında insan incelemesi tanımlı değil; "
            "GDPR Art.22 / KVKK m.11 anlamında itiraz yolu değerlendirilmeli."
            % len(onaysiz))
    return otomasyon, kontrol, inceleme


# ---------------------------------------------------------------------------
# Çalıştırma
# ---------------------------------------------------------------------------
def envanter_cikar() -> Envanter:
    env = Envanter(
        olusturma_zamani=datetime.now(UTC).isoformat(timespec="seconds"),
        proje_koku=str(PROJECT_ROOT))

    bag, i1 = tara_bagimliliklar()
    veri, i2 = tara_kisisel_veri()
    ai, i3 = tara_ai_saglayicilari()
    dis, i4 = tara_dis_cagrilar()
    oto, kon, i5 = tara_otomasyon()

    env.dependencies = bag
    env.personal_data = veri
    env.ai_providers = ai
    env.external_calls = dis
    env.automation = oto
    env.human_control = kon
    env.review_required = i1 + i2 + i3 + i4 + i5

    env.ozet = {
        "pip_paketi": sum(1 for b in bag if b.ekosistem == "pip"),
        "npm_paketi": sum(1 for b in bag if b.ekosistem == "npm"),
        "lisans_incelemesi": sum(1 for b in bag
                                 if b.risk in ("REVIEW_REQUIRED", "UNKNOWN")),
        "kurulu_degil_atlandi": sum(1 for b in bag if b.risk == "NOT_INSTALLED"),
        "copyleft": sum(1 for b in bag
                        if re.search(r"(GPL|AGPL|LGPL)", b.lisans, re.I)),
        "kisisel_veri_alani": sum(1 for v in veri if v.kategori == "KISISEL"),
        "ozel_nitelikli_aday": sum(1 for v in veri if v.kategori == "OZEL_NITELIKLI"),
        "konum_alani": sum(1 for v in veri if v.kategori == "KONUM"),
        "dogrudan_tanimlayici": sum(1 for v in veri if v.tanimlayici),
        "veri_iceren_tablo": len({v.tablo for v in veri}),
        "ai_saglayici": len(ai),
        "aktif_bulut_ai": sum(1 for a in ai if a["aktif"] and not a["yerel_mi"]),
        "dis_uc_nokta": len({c.url for c in dis}),
        "otomatik_karar": len(oto),
        "insan_kontrol": len(kon),
        "inceleme_gereken": len(env.review_required),
    }
    return env


def markdown_uret(env: Envanter) -> str:
    o = env.ozet
    sat = []
    sat.append("| Ölçüm | Değer |")
    sat.append("|---|---|")
    for k, etiket in (
            ("pip_paketi", "pip paketi"), ("npm_paketi", "npm paketi"),
            ("lisans_incelemesi", "lisans incelemesi gereken"),
            ("copyleft", "copyleft (GPL/AGPL/LGPL)"),
            ("kurulu_degil_atlandi", "kurulu olmayan opsiyonel paket"),
            ("veri_iceren_tablo", "kişisel veri içeren tablo"),
            ("kisisel_veri_alani", "kişisel veri alanı"),
            ("dogrudan_tanimlayici", "doğrudan tanımlayıcı alan"),
            ("ozel_nitelikli_aday", "özel nitelikli aday alan"),
            ("konum_alani", "konum alanı"),
            ("ai_saglayici", "AI sağlayıcı"),
            ("aktif_bulut_ai", "aktif bulut AI"),
            ("dis_uc_nokta", "dış uç nokta"),
            ("otomatik_karar", "otomatik karar noktası"),
            ("insan_kontrol", "insan kontrol noktası"),
            ("inceleme_gereken", "**insan incelemesi gereken bulgu**")):
        sat.append("| %s | %s |" % (etiket, o.get(k, "?")))
    return "\n".join(sat)


def main() -> int:
    ap = argparse.ArgumentParser(description="Van Sales uyumluluk keşif taraması")
    ap.add_argument("--markdown", action="store_true", help="özet tabloyu yazdır")
    ap.add_argument("--cikti", default=None, help="JSON çıktı dosyası")
    a = ap.parse_args()

    env = envanter_cikar()

    if a.cikti:
        Path(a.cikti).parent.mkdir(parents=True, exist_ok=True)
        Path(a.cikti).write_text(
            json.dumps(asdict(env), ensure_ascii=False, indent=1), encoding="utf-8")

    if a.markdown:
        print(markdown_uret(env))
    else:
        print(json.dumps(env.ozet, ensure_ascii=False, indent=1))
        print("\nİnceleme gereken bulgular:")
        for r in env.review_required:
            print("  · " + r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
