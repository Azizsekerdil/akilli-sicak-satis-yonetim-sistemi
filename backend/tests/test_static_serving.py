# -*- coding: utf-8 -*-
"""
SPA yakalama rotasinin dizin disina cikmadigini dogrular.

Neden ayri bir test dosyasi
---------------------------
`_spa_fallback` uygulamadaki kimlik dogrulamasi OLMAYAN uc rotadan biridir ve
yolu dogrudan istemciden alir. Ilk surumde `dist / full_path` sonucu hicbir
kapsam kontrolunden gecmiyordu; bu, derlenmis arayuzu servis eden her kurulumda
kimlik dogrulamasiz keyfi dosya okuma anlamina geliyordu:

    GET /..%2f..%2f.env          -> .env icerigi
    GET /C:/Windows/win.ini      -> sistem dosyasi

Sebep pathlib'in birlestirme davranisi: mutlak bir parca soldaki yolu tamamen
gecersiz kilar, `..` ise agactan disari cikar. Kontrol geri alinirsa bu testler
kirmizi doner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import PROJECT_ROOT

DIST = PROJECT_ROOT / "frontend" / "dist"

#: Rota yalnizca arayuz derlenmisse kayit edilir; derlenmemis ortamda
#: (temiz klon, CI) test edilecek bir sey yoktur.
pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").is_file(),
    reason="frontend/dist derlenmemis - SPA rotasi kayitli degil",
)


@pytest.fixture(scope="module")
def istemci() -> TestClient:
    from app.main import app

    with TestClient(app) as c:
        yield c


def _gizli_dosya_var() -> Path | None:
    for ad in (".env", ".env.example"):
        y = PROJECT_ROOT / ad
        if y.is_file():
            return y
    return None


@pytest.mark.parametrize(
    "yol",
    [
        "../../.env",
        "../../../.env",
        "..%2f..%2f.env",
        "....//....//.env",
        "../backend/app/core/config.py",
        "../../backend/app/main.py",
    ],
)
def test_gorece_yol_disari_cikamaz(istemci: TestClient, yol: str) -> None:
    """Yukari tirmanma denemeleri index.html'e dusmeli, dosya sizdirmamali."""
    c = istemci.get("/" + yol)
    assert c.status_code == 200                      # SPA daima index doner
    govde = c.text
    assert "VS_SECRET_KEY" not in govde
    assert "secret_key" not in govde
    assert "def create_app" not in govde


def test_mutlak_yol_disari_cikamaz(istemci: TestClient) -> None:
    """
    Mutlak yol en sinsi durum: pathlib'de `dist / "C:/Windows/win.ini"`
    dist'i tamamen atar ve dogrudan sistem dosyasini isaret eder.
    """
    for yol in ("C:/Windows/win.ini", "/etc/passwd", "C:/Windows/System32/drivers/etc/hosts"):
        c = istemci.get("/" + yol)
        assert c.status_code == 200
        assert "[fonts]" not in c.text        # win.ini imzasi
        assert "root:x:" not in c.text        # passwd imzasi


def test_proje_koku_disindaki_dosya_okunamaz(istemci: TestClient) -> None:
    """Gercek bir hedef dosyayla uctan uca dogrulama."""
    hedef = _gizli_dosya_var()
    if hedef is None:
        pytest.skip("proje kokunde .env/.env.example yok")
    imza = hedef.read_text(encoding="utf-8", errors="replace")[:200]
    c = istemci.get("/../../" + hedef.name)
    assert c.status_code == 200
    assert imza not in c.text


def test_dist_icindeki_dosya_hala_servis_ediliyor(istemci: TestClient) -> None:
    """Duzeltme mesru statik servisi bozmamali."""
    c = istemci.get("/index.html")
    assert c.status_code == 200
    assert "<div id=\"root\">" in c.text or "<!doctype html" in c.text.lower()


def test_bilinmeyen_rota_spa_kabugu_doner(istemci: TestClient) -> None:
    """Istemci tarafi yonlendirme calismaya devam etmeli."""
    c = istemci.get("/crm/customers/42")
    assert c.status_code == 200
    assert "<html" in c.text.lower()
