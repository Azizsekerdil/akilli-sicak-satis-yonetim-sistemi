"""Core layer: money maths, Turkish text handling, geo, i18n, security, logging redaction."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core import utils as u
from app.core.i18n import catalogue, missing_keys, normalize_language, t
from app.core.logging_config import redact
from app.core.security import (
    TokenError,
    create_access_token,
    decode_token,
    hash_password,
    mask_secret,
    password_strength_errors,
    verify_password,
)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
class TestMoney:
    def test_decimal_coercion_never_raises(self):
        assert u.D(None) == Decimal("0")
        assert u.D("") == Decimal("0")
        assert u.D("not a number") == Decimal("0")
        assert u.D("12.5") == Decimal("12.5")
        assert u.D(3) == Decimal("3")

    def test_money_quantises_to_four_places(self):
        assert u.money("12.345678") == Decimal("12.3457")
        assert u.money(10) == Decimal("10.0000")

    def test_money_rounds_half_up_not_bankers(self):
        # Python's default is banker's rounding; money must not do that.
        assert u.money("0.00005") == Decimal("0.0001")
        assert u.money("2.00015") == Decimal("2.0002")

    def test_vat_and_gross_round_trip(self):
        net = Decimal("100.00")
        vat = u.vat_from_net(net, 20.0)
        assert vat == Decimal("20.0000")
        assert u.net_from_gross(net + vat, 20.0) == Decimal("100.0000")

    def test_apply_percent(self):
        assert u.apply_percent(Decimal("250"), 10) == Decimal("25.0000")

    def test_pct_is_zero_safe(self):
        assert u.pct(5, 0) == 0.0
        assert u.pct(25, 100) == 25.0

    def test_safe_div(self):
        assert u.safe_div(1, 0) == 0.0
        assert u.safe_div(10, 4) == 2.5


# ---------------------------------------------------------------------------
# Turkish text
# ---------------------------------------------------------------------------
class TestTurkish:
    def test_slugify_folds_turkish_characters(self):
        assert u.slugify("Şişli Gıda Ticaret") == "sisli-gida-ticaret"
        assert u.slugify("ÇAĞLAYAN İÇECEK") == "caglayan-icecek"
        assert u.slugify("Ürün Ölçü") == "urun-olcu"

    def test_dotted_and_dotless_i_are_handled(self):
        # The classic Turkish trap: I/ı and İ/i are different letters.
        assert u.tr_upper("istanbul") == "İSTANBUL"
        assert u.tr_lower("IĞDIR") == "ığdır"

    def test_slug_never_empty(self):
        assert u.slugify("") == "item"
        assert u.slugify("!!!") == "item"

    def test_search_folding_matches_across_case(self):
        a = u.normalize_search("ŞİŞLİ Market")
        b = u.normalize_search("şişli market")
        assert a == b


# ---------------------------------------------------------------------------
# Geo
# ---------------------------------------------------------------------------
class TestGeo:
    def test_haversine_known_distance(self):
        # Istanbul (Taksim) -> Ankara (Kızılay) is roughly 350 km.
        km = u.haversine_km(41.0369, 28.9850, 39.9208, 32.8541)
        assert 340 < km < 360

    def test_zero_distance(self):
        assert u.haversine_km(41.0, 29.0, 41.0, 29.0) == pytest.approx(0.0, abs=1e-9)

    def test_road_distance_exceeds_crow_flight(self):
        straight = u.haversine_km(41.0, 29.0, 41.1, 29.1)
        road = u.road_distance_km(41.0, 29.0, 41.1, 29.1)
        assert road > straight

    def test_travel_time(self):
        assert u.travel_time_minutes(30.0, 30.0) == 60
        assert u.travel_time_minutes(10.0, 0) == 0


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
class TestDates:
    def test_month_end_handles_february_and_december(self):
        assert u.month_end(date(2026, 2, 10)) == date(2026, 2, 28)
        assert u.month_end(date(2024, 2, 10)) == date(2024, 2, 29)  # leap year
        assert u.month_end(date(2026, 12, 5)) == date(2026, 12, 31)

    def test_add_months_clamps_day(self):
        assert u.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert u.add_months(date(2026, 3, 15), -3) == date(2025, 12, 15)

    def test_date_range(self):
        r = u.date_range(date(2026, 1, 1), date(2026, 1, 5))
        assert len(r) == 5 and r[-1] == date(2026, 1, 5)
        assert u.date_range(date(2026, 1, 5), date(2026, 1, 1)) == []

    def test_parse_date_accepts_turkish_format(self):
        assert u.parse_date("15.08.2026") == date(2026, 8, 15)
        assert u.parse_date("2026-08-15") == date(2026, 8, 15)
        assert u.parse_date("nonsense") is None

    def test_hhmm_round_trip(self):
        assert u.parse_hhmm("08:30") == 510
        assert u.format_hhmm(510) == "08:30"
        assert u.parse_hhmm(None) is None

    def test_weekday_code(self):
        assert u.weekday_code(date(2026, 8, 15)) == "SAT"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
class TestJson:
    def test_decimal_and_date_serialise(self):
        s = u.dumps({"amount": Decimal("12.34"), "on": date(2026, 8, 15)})
        assert "12.34" in s and "2026-08-15" in s

    def test_turkish_characters_are_not_escaped(self):
        assert "Şişli" in u.dumps({"city": "Şişli"})

    def test_loads_is_forgiving(self):
        assert u.loads("not json", {"fallback": True}) == {"fallback": True}
        assert u.loads(None, []) == []
        assert u.loads('{"a":1}') == {"a": 1}


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
class TestSecurity:
    def test_password_hash_round_trip(self):
        h = hash_password("Sifre123!")
        assert h != "Sifre123!"
        assert verify_password("Sifre123!", h)
        assert not verify_password("wrong", h)

    def test_hashes_are_salted(self):
        assert hash_password("Same123!") != hash_password("Same123!")

    def test_long_passphrase_is_not_silently_truncated(self):
        # bcrypt clips at 72 bytes; we pre-hash so the tail still matters.
        base = "A" * 80
        h = hash_password(base + "1a!")
        assert verify_password(base + "1a!", h)
        assert not verify_password(base + "2b!", h)

    def test_turkish_password_works(self):
        h = hash_password("Şifreçğüö1!")
        assert verify_password("Şifreçğüö1!", h)

    def test_verify_is_safe_on_garbage(self):
        assert not verify_password("x", "")
        assert not verify_password("", "abc")
        assert not verify_password("x", "not-a-hash")

    def test_password_policy(self):
        assert password_strength_errors("Uzun1Sifre") == []
        assert "password.too_short" in password_strength_errors("Ab1")
        assert "password.needs_digit" in password_strength_errors("NoDigitsHere")
        assert "password.needs_uppercase" in password_strength_errors("nouppercase1")

    def test_jwt_round_trip(self):
        tok = create_access_token(42, role="SALESPERSON", scope="OWN")
        payload = decode_token(tok, expected_type="access")
        assert payload["sub"] == "42"
        assert payload["role"] == "SALESPERSON"

    def test_jwt_rejects_wrong_type(self):
        tok = create_access_token(1)
        with pytest.raises(TokenError):
            decode_token(tok, expected_type="refresh")

    def test_jwt_rejects_tampering(self):
        tok = create_access_token(1)
        with pytest.raises(TokenError):
            decode_token(tok[:-4] + "AAAA", expected_type="access")

    def test_mask_secret_keeps_only_the_last_four_characters(self):
        secret = "nvapi-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # synthetic-credential-fixture
        masked = mask_secret(secret)
        # The tail is the only recognisable part.
        assert masked.endswith("6789")
        # Nothing else survives — not the middle and not the vendor prefix.
        assert "GHIJKLMNOP" not in masked
        assert "nvapi" not in masked
        assert not masked.startswith("nvap")
        assert set(masked[:-4]) == {"*"}

    def test_mask_secret_does_not_disclose_length(self):
        short = mask_secret("A" * 20 + "TAIL")
        long_ = mask_secret("A" * 200 + "TAIL")
        assert short == long_

    def test_mask_secret_hides_a_value_shorter_than_the_kept_tail(self):
        assert mask_secret("abc") == "***"
        assert mask_secret("") == ""
        assert mask_secret(None) == ""


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------
class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "api_key=nvapi-secret-value-here-1234567890",  # synthetic-credential-fixture
            'password: "SuperSecret123"',
            "Authorization: Bearer eyJhbGciOiJI.eyJzdWIiOiIxIn0.abcdefghijk",
            "token=ghp_abcdefghijklmnopqrstuvwxyz012345",  # synthetic-credential-fixture
        ],
    )
    def test_credentials_are_stripped(self, text):
        out = redact(text)
        assert "REDACTED" in out

    def test_nvidia_and_anthropic_key_shapes(self):
        assert "nvapi-1234567890abcdef" not in redact("key is nvapi-1234567890abcdef")  # synthetic-credential-fixture
        assert "sk-ant-1234567890abcdef" not in redact("sk-ant-1234567890abcdef")  # synthetic-credential-fixture

    def test_ordinary_text_is_untouched(self):
        msg = "Satış tamamlandı: 12 koli su, 340.50 TL"
        assert redact(msg) == msg


# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------
class TestI18n:
    def test_turkish_and_english_differ(self):
        tr = t("error.not_found", "tr")
        en = t("error.not_found", "en")
        assert tr and en and tr != en

    def test_parameter_interpolation(self):
        msg = t("customer.code_taken", "tr", code="M-001")
        assert "M-001" in msg

    def test_missing_parameter_does_not_crash(self):
        assert isinstance(t("customer.code_taken", "tr"), str)

    def test_unknown_key_returns_the_key(self):
        assert t("no.such.key.at.all", "tr") == "no.such.key.at.all"

    def test_language_normalisation(self):
        assert normalize_language("en-GB") == "en"
        assert normalize_language("tr-TR,tr;q=0.9") == "tr"
        assert normalize_language("de") in ("tr", "en")
        assert normalize_language(None) in ("tr", "en")

    def test_catalogues_are_complete_in_both_languages(self):
        gaps = missing_keys()
        assert gaps["missing_in_en"] == [], f"untranslated in EN: {gaps['missing_in_en']}"
        assert gaps["missing_in_tr"] == [], f"untranslated in TR: {gaps['missing_in_tr']}"

    def test_catalogue_is_flat_and_non_empty(self):
        c = catalogue("tr")
        assert len(c) > 50
        assert all(isinstance(v, str) for v in c.values())


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
def test_chunked():
    assert u.chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert u.chunked([], 3) == []


def test_clamp():
    assert u.clamp(5, 0, 3) == 3
    assert u.clamp(-1, 0, 3) == 0
    assert u.clamp(2, 0, 3) == 2
