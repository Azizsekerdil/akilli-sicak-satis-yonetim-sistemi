"""
Keşif tarayıcısı testleri.

Bu testlerin varlık sebebi somut: sınıflandırıcının ilk sürümü alt dize
araması yapıyordu ve dört alanın dördünde de yanlış alarm verdi
(``supports_embeddings``, ``heading``, ``padding`` içindeki "din" hecesi ve
``is_healthy`` içindeki "health"). Yanlış alarm üreten bir uyumluluk
tarayıcısı okunmaz hale gelir ve gerçek bulgular gürültüde kaybolur.

Buradaki testler iki yönü birden korur: gerçek kişisel veri alanları
yakalanmalı **ve** benzeyen ama ilgisiz alanlar yakalanmamalıdır.
"""

from __future__ import annotations

import pytest

from app.compliance.scanners.discovery import (
    _kategori,
    _sozcukler,
    envanter_cikar,
)


class TestSozcukAyirma:
    def test_alt_cizgi_ile_ayirir(self):
        assert _sozcukler("national_id") == {"national", "id"}

    def test_camel_case_ile_ayirir(self):
        assert _sozcukler("lastLoginIp") == {"last", "login", "ip"}

    def test_tek_sozcuk(self):
        assert _sozcukler("email") == {"email"}


class TestKisiselVeriTespiti:
    @pytest.mark.parametrize(
        "alan",
        ["email", "phone", "mobile", "full_name", "contact_person",
         "national_id", "tax_number", "ip_address", "user_agent",
         "password_hash", "plate_number", "postal_code", "birth_date"],
    )
    def test_gercek_kisisel_veri_yakalanir(self, alan):
        kategori, _ = _kategori(alan)
        assert kategori == "KISISEL", f"{alan} kişisel veri olarak görülmedi"

    @pytest.mark.parametrize(
        "alan",
        ["latitude", "longitude", "last_lat", "last_lng", "accuracy_m",
         "arrival_lat", "geofence_distance_m"],
    )
    def test_konum_ayri_kategoride(self, alan):
        kategori, _ = _kategori(alan)
        assert kategori == "KONUM", f"{alan} konum olarak görülmedi"

    @pytest.mark.parametrize(
        "alan",
        ["health_data", "biometric_template", "genetic_data",
         "criminal_record", "union_membership", "religious_belief",
         "sabika_kaydi", "kan_grubu"],
    )
    def test_ozel_nitelikli_yakalanir(self, alan):
        kategori, _ = _kategori(alan)
        assert kategori == "OZEL_NITELIKLI", f"{alan} özel nitelikli görülmedi"


class TestYanlisAlarmYok:
    """İlk sürümde bu alanların hepsi yanlış eşleşmişti."""

    @pytest.mark.parametrize(
        "alan",
        [
            "supports_embeddings",   # "embeddings" içindeki "din"
            "heading",               # "heading" içindeki "din"
            "padding",               # "padding" içindeki "din"
            "is_healthy",            # "healthy" ile "health"
            "reading",               # "din"
            "loading",               # "din"
            "pending",               # "din"
            "rounding",              # "din"
            "quantity",
            "total_amount",
            "warehouse_id",
            "movement_type",
            "vat_rate",
            "is_active",
            "created_at",
            "sort_order",
        ],
    )
    def test_ilgisiz_alan_isaretlenmez(self, alan):
        kategori, _ = _kategori(alan)
        assert kategori == "DIGER", (
            f"{alan} yanlışlıkla {kategori} olarak işaretlendi — "
            "sözcük temelli eşleşme bozulmuş olabilir"
        )


class TestDogrudanTanimlayici:
    @pytest.mark.parametrize(
        "alan", ["email", "phone", "national_id", "tax_number", "iban",
                 "password_hash", "ip_address"],
    )
    def test_dogrudan_tanimlayici_isaretlenir(self, alan):
        _, tanimlayici = _kategori(alan)
        assert tanimlayici is True, f"{alan} doğrudan tanımlayıcı sayılmadı"

    @pytest.mark.parametrize("alan", ["city", "district", "latitude", "gender"])
    def test_dolayli_alan_tanimlayici_sayilmaz(self, alan):
        _, tanimlayici = _kategori(alan)
        assert tanimlayici is False, f"{alan} yanlışlıkla doğrudan tanımlayıcı"


class TestEnvanter:
    """Tarama gerçekten çalışıyor ve boş dönmüyor mu?"""

    @pytest.fixture(scope="class")
    def envanter(self):
        return envanter_cikar()

    def test_bagimlilik_bulunur(self, envanter):
        assert envanter.ozet["pip_paketi"] > 20

    def test_kisisel_veri_bulunur(self, envanter):
        # Van Sales bir CRM içerir; sıfır kişisel veri alanı bulmak
        # taramanın çalışmadığı anlamına gelir.
        assert envanter.ozet["kisisel_veri_alani"] > 30
        assert envanter.ozet["veri_iceren_tablo"] > 10

    def test_musteri_tablosu_envanterde(self, envanter):
        tablolar = {v.tablo for v in envanter.personal_data}
        assert "customers" in tablolar
        assert "users" in tablolar

    def test_ai_saglayicilari_listelenir(self, envanter):
        assert len(envanter.ai_providers) == 3
        adlar = {a["ad"] for a in envanter.ai_providers}
        assert "LM Studio" in adlar

    def test_api_anahtari_asla_raporlanmaz(self, envanter):
        """Envanter yalnızca 'anahtar tanımlı mı' bilgisini taşımalı."""
        for saglayici in envanter.ai_providers:
            assert isinstance(saglayici["anahtar_tanimli"], bool)
            for deger in saglayici.values():
                metin = str(deger)
                assert "nvapi-" not in metin
                assert "sk-ant-" not in metin

    def test_yerel_saglayici_yurt_disi_aktarim_uretmez(self, envanter):
        yerel = [a for a in envanter.ai_providers if a["yerel_mi"]]
        assert yerel, "yerel sağlayıcı bulunamadı"
        for a in yerel:
            assert a["yurt_disi_aktarim"] == "HAYIR"

    def test_bilinmeyen_otomatik_uygun_sayilmaz(self, envanter):
        """UNKNOWN riskli paket varsa mutlaka inceleme listesine düşmeli."""
        bilinmeyen = [b for b in envanter.dependencies if b.risk == "UNKNOWN"]
        if bilinmeyen:
            assert envanter.review_required, (
                "UNKNOWN lisans var ama inceleme listesi boş — "
                "bilinmeyen sessizce uygun sayılmış olabilir"
            )

    def test_insan_kontrol_noktalari_bulunur(self, envanter):
        assert envanter.ozet["insan_kontrol"] > 0


class TestTabloBaglami:
    """
    ``name`` gibi genel alanlar tabloya göre anlam değiştirir.

    ``products.name`` bir ürün adıdır, ``users.name`` bir kişi adıdır. İkisini
    aynı kefeye koymak envanteri şişirir ve gerçek bulguları gürültüye gömer.
    """

    @pytest.mark.parametrize("tablo", ["users", "customers", "salespersons"])
    def test_kisi_tablosunda_name_kisiseldir(self, tablo):
        kategori, _ = _kategori("name", tablo)
        assert kategori == "KISISEL"

    @pytest.mark.parametrize(
        "tablo", ["products", "roles", "campaigns", "warehouses", "routes"]
    )
    def test_kisi_disi_tabloda_name_kisisel_degildir(self, tablo):
        kategori, _ = _kategori("name", tablo)
        assert kategori == "DIGER", (
            f"{tablo}.name kişisel veri sayıldı — bu bir varlık adı, kişi adı değil"
        )

    def test_tablo_verilmezse_genel_name_isaretlenmez(self):
        assert _kategori("name")[0] == "DIGER"


class TestParolaMetaVerisi:
    """Parola alanı kimlik belirler; parolayla ilgili meta veri belirlemez."""

    @pytest.mark.parametrize("alan", ["password", "password_hash"])
    def test_parolanin_kendisi_tanimlayicidir(self, alan):
        kategori, tanimlayici = _kategori(alan, "users")
        assert kategori == "KISISEL" and tanimlayici is True

    @pytest.mark.parametrize(
        "alan", ["password_changed_at", "must_change_password", "password_min_length"]
    )
    def test_parola_meta_verisi_tanimlayici_degildir(self, alan):
        kategori, tanimlayici = _kategori(alan, "users")
        assert kategori == "DIGER" and tanimlayici is False
