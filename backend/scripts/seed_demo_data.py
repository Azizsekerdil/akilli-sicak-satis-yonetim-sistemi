"""
Synthetic demo dataset generator.

Creates a realistic — but entirely **fictional** — Turkish food & beverage
distribution business so every module, report, statistic and AI feature can be
exercised against data that behaves like the real thing.

    python -m scripts.seed_demo_data                 # default sizes
    python -m scripts.seed_demo_data --customers 200 --months 6
    python -m scripts.seed_demo_data --reset         # wipe demo rows first

Nothing here is real customer data.  Every name, address and coordinate is
generated, and the whole set is tagged ``DEMO`` so it can be told apart from
production records and removed wholesale.

Stock integrity
---------------
History is not faked into the balances.  Each product receives an OPENING
receipt large enough to cover everything the simulation will consume, every
historical sale posts a real negative movement, and the materialised balances
are rebuilt from the ledger at the end.  So the demo database satisfies the
same invariant as production: ``sum(movements) == stock_balances``.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import create_all, session_scope  # noqa: E402
from app.core.enums import (  # noqa: E402
    CampaignScope,
    CampaignStatus,
    CampaignType,
    CustomerStatus,
    CustomerType,
    DocumentType,
    InvoiceStatus,
    LedgerEntryType,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    ProductStatus,
    RoleCode,
    SalesChannel,
    StockMovementType,
    StockStatus,
    UnitOfMeasure,
    VehicleType,
    VisitFrequency,
    VisitOutcome,
    WarehouseType,
)
from app.core.security import hash_password  # noqa: E402
from app.core.utils import money, qty, slugify  # noqa: E402
from app.models.auth import Role, User  # noqa: E402
from app.models.base import utcnow  # noqa: E402
from app.models.campaign import Campaign, CampaignCondition  # noqa: E402
from app.models.customer import Customer, CustomerLedger  # noqa: E402
from app.models.organization import Branch, Company, Region  # noqa: E402
from app.models.product import (  # noqa: E402
    Barcode,
    Brand,
    PriceList,
    Product,
    ProductCategory,
    ProductUnit,
)
from app.models.route import Route, RouteStop, Visit  # noqa: E402
from app.models.sales import (  # noqa: E402
    Invoice,
    InvoiceItem,
    Order,
    OrderItem,
    Payment,
    PaymentAllocation,
    Sale,
    SaleItem,
)
from app.models.system import NumberSequence  # noqa: E402
from app.models.vehicle import Salesperson, Vehicle  # noqa: E402
from app.models.warehouse import Lot, StockBalance, StockMovement, Warehouse  # noqa: E402
from app.services import bootstrap_service  # noqa: E402

#: Deterministic by default so two runs produce comparable numbers.
SEED = 20260818
DEMO_TAG = "DEMO"

# ---------------------------------------------------------------------------
# Reference data (fictional)
# ---------------------------------------------------------------------------
REGIONS = [
    ("MRM", "Marmara", 40.98, 29.02),
    ("EGE", "Ege", 38.42, 27.14),
    ("AKD", "Akdeniz", 36.90, 30.70),
    ("ICA", "İç Anadolu", 39.93, 32.86),
]

CITIES = {
    "MRM": [("İstanbul", 41.01, 28.98), ("Kocaeli", 40.77, 29.95), ("Bursa", 40.19, 29.06)],
    "EGE": [("İzmir", 38.42, 27.14), ("Manisa", 38.62, 27.43), ("Aydın", 37.85, 27.84)],
    "AKD": [("Antalya", 36.90, 30.70), ("Mersin", 36.80, 34.63), ("Adana", 37.00, 35.32)],
    "ICA": [("Ankara", 39.93, 32.86), ("Konya", 37.87, 32.49), ("Kayseri", 38.73, 35.49)],
}

DISTRICTS = [
    "Merkez", "Yeni Mahalle", "Cumhuriyet", "Bahçelievler", "Fatih", "Çamlık",
    "Gültepe", "Yeşilyurt", "Atatürk", "İstiklal", "Barbaros", "Kordon",
]

CATEGORIES = [
    ("ICE", "İçecek", "Beverages"),
    ("SU", "Su", "Water"),
    ("SUT", "Süt Ürünleri", "Dairy"),
    ("ATS", "Atıştırmalık", "Snacks"),
    ("BSK", "Bisküvi & Kek", "Biscuits & Cake"),
    ("DON", "Dondurulmuş", "Frozen"),
]

BRANDS = [
    ("PNR", "Pınaros"), ("AKD", "Akdeniz Gıda"), ("TZE", "Taze Vadi"),
    ("GNS", "Günseli"), ("MRT", "Marmaris"), ("ZRV", "Zirve"),
]

#: (category, brand, name, base unit, units per case, price, vat, shelf life days, storage)
PRODUCT_TEMPLATES = [
    ("ICE", "Kola {v} ml", "PIECE", 24, 12.50, 20.0, 270, "AMBIENT", (250, 330, 500, 1000)),
    ("ICE", "Gazoz {v} ml", "PIECE", 24, 11.00, 20.0, 270, "AMBIENT", (250, 330, 500)),
    ("ICE", "Meyve Suyu {v} ml", "PIECE", 12, 18.75, 20.0, 210, "AMBIENT", (200, 330, 1000)),
    ("ICE", "Soğuk Çay {v} ml", "PIECE", 12, 16.00, 20.0, 240, "AMBIENT", (330, 500, 1000)),
    ("SU", "Su {v} ml", "PIECE", 12, 4.50, 1.0, 730, "AMBIENT", (330, 500, 1500, 5000)),
    ("SU", "Maden Suyu {v} ml", "PIECE", 24, 7.25, 20.0, 540, "AMBIENT", (200, 250, 330)),
    ("SUT", "Ayran {v} ml", "PIECE", 24, 9.90, 1.0, 21, "CHILLED", (200, 250, 300, 1000)),
    ("SUT", "Süt {v} ml", "PIECE", 12, 22.00, 1.0, 120, "AMBIENT", (200, 500, 1000)),
    ("SUT", "Yoğurt {v} g", "PIECE", 8, 34.50, 1.0, 25, "CHILLED", (500, 1000, 1500)),
    ("SUT", "Kefir {v} ml", "PIECE", 12, 28.00, 1.0, 21, "CHILLED", (250, 500, 1000)),
    ("ATS", "Cips {v} g", "PIECE", 24, 15.00, 20.0, 180, "AMBIENT", (35, 65, 110, 160)),
    ("ATS", "Kraker {v} g", "PIECE", 24, 8.75, 20.0, 240, "AMBIENT", (40, 70, 100)),
    ("ATS", "Kuruyemiş {v} g", "PIECE", 12, 42.00, 20.0, 300, "AMBIENT", (80, 150, 250)),
    ("BSK", "Bisküvi {v} g", "PIECE", 24, 10.25, 20.0, 300, "AMBIENT", (80, 120, 200)),
    ("BSK", "Kek {v} g", "PIECE", 24, 12.00, 20.0, 120, "AMBIENT", (40, 55, 70)),
    ("BSK", "Gofret {v} g", "PIECE", 24, 7.50, 20.0, 210, "AMBIENT", (35, 45, 60)),
    ("DON", "Dondurma {v} ml", "PIECE", 12, 26.00, 20.0, 365, "FROZEN", (100, 500, 900)),
]

CUSTOMER_TYPE_WEIGHTS = [
    (CustomerType.GROCERY, 34), (CustomerType.MARKET, 20), (CustomerType.SUPERMARKET, 6),
    (CustomerType.RESTAURANT, 8), (CustomerType.CAFE, 7), (CustomerType.KIOSK, 6),
    (CustomerType.CANTEEN, 4), (CustomerType.HOTEL, 3), (CustomerType.GAS_STATION, 3),
    (CustomerType.SCHOOL, 2), (CustomerType.WHOLESALER, 3), (CustomerType.HORECA, 4),
]

CHANNEL_BY_TYPE = {
    CustomerType.GROCERY: SalesChannel.TRADITIONAL,
    CustomerType.MARKET: SalesChannel.TRADITIONAL,
    CustomerType.SUPERMARKET: SalesChannel.MODERN,
    CustomerType.RESTAURANT: SalesChannel.HORECA,
    CustomerType.CAFE: SalesChannel.HORECA,
    CustomerType.HOTEL: SalesChannel.HORECA,
    CustomerType.HORECA: SalesChannel.HORECA,
    CustomerType.KIOSK: SalesChannel.TRADITIONAL,
    CustomerType.CANTEEN: SalesChannel.INSTITUTIONAL,
    CustomerType.SCHOOL: SalesChannel.INSTITUTIONAL,
    CustomerType.GAS_STATION: SalesChannel.TRADITIONAL,
    CustomerType.WHOLESALER: SalesChannel.WHOLESALE,
}

SHOP_PREFIX = [
    "Öz", "Yeni", "Güven", "Bereket", "Şafak", "Umut", "Doğuş", "Anadolu",
    "Efe", "Deniz", "Gül", "Çınar", "Kardeşler", "Merkez", "Yıldız", "Ege",
]
SHOP_SUFFIX = [
    "Market", "Gıda", "Bakkal", "Süpermarket", "Büfe", "Şarküteri",
    "Ticaret", "Mağaza", "Kantin", "Kafe",
]

FIRST_NAMES = [
    "Ahmet", "Mehmet", "Mustafa", "Ali", "Hüseyin", "Hasan", "İbrahim", "Osman",
    "Ayşe", "Fatma", "Emine", "Hatice", "Zeynep", "Elif", "Merve", "Büşra",
]
LAST_NAMES = [
    "Yılmaz", "Kaya", "Demir", "Şahin", "Çelik", "Yıldız", "Yıldırım", "Öztürk",
    "Aydın", "Özdemir", "Arslan", "Doğan", "Kılıç", "Aslan", "Çetin", "Kara",
]

PLATES = ["34", "35", "07", "06", "16", "41", "42", "45", "01", "38"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def person_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


# ---------------------------------------------------------------------------
# Contact details — masked at the source
# ---------------------------------------------------------------------------
# Demo rows end up on screenshots, in exported reports and in the presentation
# deck.  Generating *plausible* Turkish numbers there was the problem: a random
# 05xx number looks exactly like a real one, and nobody downstream can tell
# whether the person on the other end consented to appear in a slide.  Every
# number this generator produces is therefore masked at the point of creation,
# so no unmasked value ever exists to leak — masking a screenshot afterwards is
# a step somebody eventually forgets.
#
# The shape stays realistic enough to exercise the UI's column widths, search
# and formatting, but the subscriber digits are the literal letter ``X`` and
# the number cannot be dialled.
def demo_mobile(rng: random.Random) -> str:
    """A masked, undialable mobile number: ``+90 5XX XXX XX 42``."""
    return f"+90 5XX XXX XX {rng.randint(0, 99):02d}"


def demo_landline(rng: random.Random) -> str:
    """A masked, undialable landline number: ``+90 2XX XXX XX 42``."""
    return f"+90 2XX XXX XX {rng.randint(0, 99):02d}"


def demo_tax_number(rng: random.Random) -> str:
    """
    A clearly synthetic tax number.

    Ten random digits look like a real VKN and would be indistinguishable from
    one in a screenshot.  The fixed ``0000`` block makes the value obviously
    fabricated while keeping the field the right length and shape.
    """
    return f"0000{rng.randint(0, 999_999):06d}"


def demo_email(rng: random.Random, local: str) -> str:  # noqa: ARG001 - rng kept for symmetry
    """
    An address on a reserved, unresolvable domain.

    ``.invalid`` is set aside by RFC 2606 precisely so examples cannot reach a
    real mailbox, which ``.local`` (an mDNS name) does not guarantee.
    """
    return f"{local}@demo.invalid"


def jitter(rng: random.Random, base: float, spread: float) -> float:
    return base + rng.uniform(-spread, spread)


def weighted_choice(rng: random.Random, pairs: list[tuple[str, int]]) -> str:
    total = sum(w for _, w in pairs)
    r = rng.uniform(0, total)
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if r <= acc:
            return value
    return pairs[-1][0]


def seasonal_factor(d: date, storage: str) -> float:
    """
    Beverages sell far more in summer; frozen even more so.

    Modelled as a cosine peaking in July — crude, but it gives the forecasting
    module a real seasonal signal to find instead of noise.
    """
    day_of_year = d.timetuple().tm_yday
    summer = math.cos((day_of_year - 196) / 365 * 2 * math.pi)
    if storage == "FROZEN":
        return 1.0 + 0.55 * summer
    if storage == "CHILLED":
        return 1.0 + 0.30 * summer
    return 1.0 + 0.18 * summer


def weekday_factor(d: date) -> float:
    # Friday/Saturday are the heavy replenishment days; Sunday is quiet.
    return [0.95, 0.90, 1.00, 1.05, 1.25, 1.20, 0.45][d.weekday()]


# ---------------------------------------------------------------------------
# Wipe
# ---------------------------------------------------------------------------
DEMO_TABLES = [
    PaymentAllocation, Payment, InvoiceItem, Invoice, SaleItem, Sale,
    OrderItem, Order, Visit, RouteStop, Route, CustomerLedger, Customer,
    StockMovement, StockBalance, Lot, CampaignCondition, Campaign,
    Barcode, ProductUnit, Product, ProductCategory, Brand, PriceList,
    Vehicle, Salesperson, Warehouse, Branch, Region, Company, NumberSequence,
]


def wipe(db: Session) -> None:
    """Remove generated content.  Users/roles/settings are left alone."""
    for model in DEMO_TABLES:
        db.execute(delete(model))
    db.execute(delete(User).where(User.username.like("demo_%")))
    db.commit()
    print("  previous demo data removed")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def seed(
    *,
    customers: int = 500,
    products: int = 100,
    salespeople: int = 10,
    warehouses: int = 3,
    routes: int = 30,
    months: int = 12,
    reset: bool = False,
    seed_value: int = SEED,
) -> dict[str, int]:
    rng = random.Random(seed_value)
    stats: dict[str, int] = {}

    create_all()
    bootstrap_service.ensure_baseline()

    with session_scope() as db:
        if reset:
            wipe(db)

        existing = db.execute(select(func.count(Customer.id))).scalar_one() or 0
        if existing and not reset:
            print(f"  {existing} customers already present — pass --reset to regenerate")
            return {"skipped": 1}

        # --- Company / regions ------------------------------------------------
        company = Company(
            code="DEMO", name="Demo Gıda ve İçecek A.Ş.", legal_name="Demo Gıda ve İçecek A.Ş.",
            tax_office="Kadıköy", tax_number="1234567890", city="İstanbul",
            country="Türkiye", currency="TRY", default_vat_rate=20.0,
            description=DEMO_TAG,
        )
        db.add(company)
        db.flush()

        region_rows: list[Region] = []
        for code, name, lat, lng in REGIONS:
            r = Region(
                company_id=company.id, code=code, name=name, name_en=name,
                city=CITIES[code][0][0], center_lat=lat, center_lng=lng,
                description=DEMO_TAG,
            )
            db.add(r)
            region_rows.append(r)
        db.flush()
        stats["regions"] = len(region_rows)

        # --- Warehouses -------------------------------------------------------
        wh_rows: list[Warehouse] = []
        central = Warehouse(
            company_id=company.id, code="WH-MERKEZ", name="Merkez Depo",
            name_en="Central Warehouse", warehouse_type=WarehouseType.CENTRAL,
            region_id=region_rows[0].id, city="İstanbul", latitude=41.02, longitude=28.95,
            capacity_volume_l=2_000_000, capacity_weight_kg=900_000,
            allocation_strategy="FEFO", description=DEMO_TAG,
        )
        db.add(central)
        wh_rows.append(central)
        for i in range(1, max(1, warehouses)):
            reg = region_rows[i % len(region_rows)]
            w = Warehouse(
                company_id=company.id, code=f"WH-{reg.code}", name=f"{reg.name} Bölge Deposu",
                name_en=f"{reg.name} Regional Warehouse", warehouse_type=WarehouseType.REGIONAL,
                region_id=reg.id, city=reg.city, latitude=reg.center_lat,
                longitude=reg.center_lng, capacity_volume_l=600_000,
                capacity_weight_kg=250_000, allocation_strategy="FEFO", description=DEMO_TAG,
            )
            db.add(w)
            wh_rows.append(w)
        db.flush()
        stats["warehouses"] = len(wh_rows)

        # --- Categories / brands ---------------------------------------------
        cat_rows = {}
        for code, tr, en in CATEGORIES:
            c = ProductCategory(code=code, name=tr, name_en=en, level=1, description=DEMO_TAG)
            db.add(c)
            cat_rows[code] = c
        brand_rows = []
        for code, name in BRANDS:
            b = Brand(code=code, name=name, name_en=name, manufacturer=name, description=DEMO_TAG)
            db.add(b)
            brand_rows.append(b)
        db.flush()

        price_list = PriceList(
            code="STD", name="Standart Fiyat Listesi", name_en="Standard Price List",
            currency="TRY", is_default=True, priority=100, description=DEMO_TAG,
        )
        db.add(price_list)
        db.flush()

        # --- Products ---------------------------------------------------------
        product_rows: list[Product] = []
        combos: list[tuple] = []
        for tpl in PRODUCT_TEMPLATES:
            cat, pattern, base_uom, per_case, price, vat, shelf, storage, variants = tpl
            for v in variants:
                for brand in brand_rows:
                    combos.append((cat, pattern, base_uom, per_case, price, vat, shelf, storage, v, brand))
        rng.shuffle(combos)

        for idx, (cat, pattern, base_uom, per_case, price, vat, shelf, storage, v, brand) in enumerate(
            combos[:products]
        ):
            name = f"{brand.name} {pattern.format(v=v)}"
            unit_price = round(price * (0.85 + rng.random() * 0.4), 2)
            cost = round(unit_price * rng.uniform(0.62, 0.78), 2)
            unit_vol = v / 1000.0 if "ml" in pattern else v / 1000.0
            unit_wt = unit_vol * (1.03 if "Su" in pattern else 1.0)

            p = Product(
                sku=f"SKU{idx + 1:05d}",
                code=f"SKU{idx + 1:05d}",
                name=name,
                name_en=name,
                short_name=pattern.format(v=v)[:96],
                category_id=cat_rows[cat].id,
                brand_id=brand.id,
                status=ProductStatus.ACTIVE,
                base_uom=UnitOfMeasure.PIECE if base_uom == "PIECE" else base_uom,
                sales_uom=UnitOfMeasure.CASE,
                units_per_case=Decimal(per_case),
                unit_volume_l=round(unit_vol, 4),
                unit_weight_kg=round(unit_wt, 4),
                case_volume_l=round(unit_vol * per_case * 1.12, 4),
                case_weight_kg=round(unit_wt * per_case * 1.04, 4),
                storage_condition=storage,
                is_lot_tracked=True,
                shelf_life_days=shelf,
                min_remaining_shelf_life_days=max(3, shelf // 6),
                purchase_price=money(cost * 0.96),
                cost_price=money(cost),
                sale_price=money(unit_price),
                recommended_retail_price=money(unit_price * 1.28),
                vat_rate=vat,
                max_discount_percent=20.0,
                min_stock_level=qty(per_case * 5),
                reorder_point=qty(per_case * 12),
                is_sellable=True,
                description=DEMO_TAG,
            )
            db.add(p)
            db.flush()

            db.add(ProductUnit(
                product_id=p.id, uom=UnitOfMeasure.CASE, factor=Decimal(per_case),
                is_default_sales_unit=True, price=money(unit_price * per_case),
                volume_l=p.case_volume_l, weight_kg=p.case_weight_kg,
            ))
            db.add(Barcode(
                product_id=p.id, barcode=f"869{idx + 1:010d}",
                uom=UnitOfMeasure.PIECE, is_primary=True,
            ))
            product_rows.append(p)
        db.flush()
        stats["products"] = len(product_rows)

        # --- Users, salespeople, vehicles -------------------------------------
        sp_role = db.execute(
            select(Role).where(Role.code == RoleCode.SALESPERSON)
        ).scalar_one()

        sp_rows: list[Salesperson] = []
        veh_rows: list[Vehicle] = []
        for i in range(salespeople):
            reg = region_rows[i % len(region_rows)]
            full_name = person_name(rng)
            username = f"demo_plasiyer{i + 1}"
            user = User(
                username=username,
                password_hash=hash_password("Demo1234!"),
                full_name=full_name,
                email=demo_email(rng, slugify(full_name)),
                role_id=sp_role.id,
                region_id=reg.id,
                company_id=company.id,
                language="tr",
                password_changed_at=utcnow(),
            )
            db.add(user)
            db.flush()

            van_wh = Warehouse(
                company_id=company.id,
                code=f"VH-{PLATES[i % len(PLATES)]}{100 + i}",
                name=f"Araç Deposu {i + 1}",
                name_en=f"Vehicle Warehouse {i + 1}",
                warehouse_type=WarehouseType.VEHICLE,
                region_id=reg.id,
                capacity_volume_l=9000,
                capacity_weight_kg=3500,
                allocation_strategy="FEFO",
                description=DEMO_TAG,
            )
            db.add(van_wh)
            db.flush()

            vehicle = Vehicle(
                code=f"ARAC{i + 1:02d}",
                plate_number=f"{PLATES[i % len(PLATES)]} DM {1000 + i}",
                name=f"Satış Aracı {i + 1}",
                warehouse_id=van_wh.id,
                home_warehouse_id=wh_rows[i % len(wh_rows)].id,
                region_id=reg.id,
                vehicle_type=VehicleType.REFRIGERATED if i % 3 == 0 else VehicleType.VAN,
                is_refrigerated=i % 3 == 0,
                brand=rng.choice(["Ford", "Mercedes", "Iveco", "Renault"]),
                model=rng.choice(["Transit", "Sprinter", "Daily", "Master"]),
                model_year=rng.randint(2018, 2025),
                capacity_volume_l=9000,
                capacity_weight_kg=3500,
                capacity_cases=420,
                odometer_km=rng.randint(20_000, 220_000),
                insurance_expiry=date.today() + timedelta(days=rng.randint(10, 300)),
                inspection_expiry=date.today() + timedelta(days=rng.randint(10, 400)),
                last_lat=jitter(rng, reg.center_lat, 0.15),
                last_lng=jitter(rng, reg.center_lng, 0.15),
                last_position_at=utcnow(),
                notes=DEMO_TAG,
            )
            db.add(vehicle)
            db.flush()

            sp = Salesperson(
                code=f"PL{i + 1:03d}",
                user_id=user.id,
                full_name=full_name,
                phone=demo_mobile(rng),
                email=user.email,
                region_id=reg.id,
                default_vehicle_id=vehicle.id,
                default_warehouse_id=wh_rows[i % len(wh_rows)].id,
                hire_date=date.today() - timedelta(days=rng.randint(200, 2600)),
                commission_percent=round(rng.uniform(1.0, 3.5), 2),
                max_discount_percent=round(rng.uniform(5, 15), 1),
                cash_limit=money(rng.choice([25_000, 50_000, 75_000])),
                notes=DEMO_TAG,
            )
            db.add(sp)
            db.flush()
            vehicle.default_salesperson_id = sp.id
            sp_rows.append(sp)
            veh_rows.append(vehicle)
        db.flush()
        stats["salespeople"] = len(sp_rows)
        stats["vehicles"] = len(veh_rows)

        # --- Customers --------------------------------------------------------
        cust_rows: list[Customer] = []
        weekdays = ["MON", "TUE", "WED", "THU", "FRI", "SAT"]
        for i in range(customers):
            reg = region_rows[i % len(region_rows)]
            city, clat, clng = rng.choice(CITIES[reg.code])
            ctype = weighted_choice(rng, CUSTOMER_TYPE_WEIGHTS)
            name = f"{rng.choice(SHOP_PREFIX)} {rng.choice(SHOP_SUFFIX)}"
            sp = sp_rows[i % len(sp_rows)]

            size = {
                CustomerType.SUPERMARKET: 3.4, CustomerType.WHOLESALER: 4.2,
                CustomerType.MARKET: 1.8, CustomerType.HOTEL: 2.2,
                CustomerType.RESTAURANT: 1.5, CustomerType.SCHOOL: 1.3,
            }.get(ctype, 1.0)

            credit = money(round(rng.uniform(5_000, 40_000) * size, -2))
            visit_days = rng.sample(weekdays, k=rng.choice([1, 1, 2, 2, 3]))

            c = Customer(
                code=f"M{i + 1:05d}",
                name=f"{name} - {person_name(rng)}",
                trade_name=name,
                customer_type=ctype,
                channel=CHANNEL_BY_TYPE.get(ctype, SalesChannel.TRADITIONAL),
                status=CustomerStatus.ACTIVE if rng.random() > 0.05 else CustomerStatus.PASSIVE,
                tax_office=city,
                tax_number=demo_tax_number(rng),
                address=f"{rng.choice(DISTRICTS)} Mah. {rng.randint(1, 200)}. Sok. No:{rng.randint(1, 90)}",
                city=city,
                district=rng.choice(DISTRICTS),
                latitude=round(jitter(rng, clat, 0.16), 6),
                longitude=round(jitter(rng, clng, 0.20), 6),
                region_id=reg.id,
                phone=demo_landline(rng),
                contact_person=person_name(rng),
                default_salesperson_id=sp.id,
                visit_frequency=rng.choice(
                    [VisitFrequency.WEEKLY, VisitFrequency.WEEKLY,
                     VisitFrequency.TWICE_WEEKLY, VisitFrequency.BIWEEKLY]
                ),
                visit_days=",".join(visit_days),
                visit_sequence=i % 40,
                service_time_minutes=rng.choice([8, 10, 12, 15, 20]),
                opening_time="08:00",
                closing_time="20:00",
                is_priority=rng.random() < 0.08,
                price_list_id=price_list.id,
                payment_method=rng.choice(
                    [PaymentMethod.CASH, PaymentMethod.CASH,
                     PaymentMethod.OPEN_ACCOUNT, PaymentMethod.CREDIT_CARD]
                ),
                payment_term_days=rng.choice([0, 0, 7, 14, 30]),
                credit_limit=credit,
                risk_limit=money(credit * Decimal("1.2")),
                discount_percent=round(rng.choice([0, 0, 0, 2, 3, 5]), 1),
                notes=DEMO_TAG,
            )
            db.add(c)
            cust_rows.append(c)
        db.flush()
        stats["customers"] = len(cust_rows)

        # --- Campaigns --------------------------------------------------------
        today = date.today()
        camp_defs = [
            ("KMP-10A1", "10 Koli Al 1 Koli Bedava", CampaignType.BUY_X_GET_Y, 10, 0.0),
            ("KMP-5P5", "5 Koli Üzeri %5 İndirim", CampaignType.QUANTITY_DISCOUNT, 5, 5.0),
            ("KMP-20K3", "20.000 TL Üzeri %3 İndirim", CampaignType.VALUE_DISCOUNT, 20000, 3.0),
            ("KMP-MIX", "3 Farklı Ürün %4 Ekstra", CampaignType.BASKET_MIX, 3, 4.0),
        ]
        for code, name, ctype2, threshold, percent in camp_defs:
            camp = Campaign(
                code=code, name=name, name_en=name, campaign_type=ctype2,
                status=CampaignStatus.ACTIVE,
                start_date=today - timedelta(days=60),
                end_date=today + timedelta(days=120),
                scope=CampaignScope.ALL,
                discount_percent=percent,
                free_product_id=product_rows[0].id if ctype2 == CampaignType.BUY_X_GET_Y else None,
                free_quantity=Decimal("1") if ctype2 == CampaignType.BUY_X_GET_Y else Decimal("0"),
                free_uom=UnitOfMeasure.CASE if ctype2 == CampaignType.BUY_X_GET_Y else None,
                priority=100, description=DEMO_TAG,
            )
            db.add(camp)
            db.flush()
            db.add(CampaignCondition(
                campaign_id=camp.id,
                subject="ORDER",
                metric="AMOUNT" if ctype2 == CampaignType.VALUE_DISCOUNT
                else ("DISTINCT_PRODUCTS" if ctype2 == CampaignType.BASKET_MIX else "QUANTITY"),
                min_value=Decimal(str(threshold)),
                step_value=Decimal(str(threshold)) if ctype2 == CampaignType.BUY_X_GET_Y else None,
            ))
        db.flush()
        stats["campaigns"] = len(camp_defs)

        # --- Routes -----------------------------------------------------------
        route_rows: list[Route] = []
        per_route = max(4, len(cust_rows) // max(1, routes))
        for i in range(routes):
            sp = sp_rows[i % len(sp_rows)]
            veh = veh_rows[i % len(veh_rows)]
            wd = weekdays[i % len(weekdays)]
            r = Route(
                code=f"ROT{i + 1:03d}",
                name=f"{sp.full_name} - {wd}",
                is_template=True,
                weekday=wd,
                salesperson_id=sp.id,
                vehicle_id=veh.id,
                region_id=sp.region_id,
                start_warehouse_id=veh.home_warehouse_id,
                end_warehouse_id=veh.home_warehouse_id,
                planned_start_time="08:30",
                description=DEMO_TAG,
            )
            db.add(r)
            db.flush()
            members = cust_rows[i * per_route : (i + 1) * per_route]
            for seq, cust in enumerate(members, start=1):
                db.add(RouteStop(
                    route_id=r.id, customer_id=cust.id, sequence=seq,
                    service_time_minutes=cust.service_time_minutes,
                    is_priority=cust.is_priority,
                ))
                cust.default_route_id = r.id
            r.planned_stops = len(members)
            route_rows.append(r)
        db.flush()
        stats["routes"] = len(route_rows)

        # --- Targets ----------------------------------------------------------
        from app.core.enums import TargetMetric, TargetPeriod, TargetSubject
        from app.models.analytics import Target

        today0 = date.today()
        for offset in range(-2, 1):
            pstart = date(today0.year, today0.month, 1)
            for _ in range(abs(offset)):
                pstart = (pstart - timedelta(days=1)).replace(day=1)
            pend = (pstart.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            for sp in sp_rows:
                db.add(Target(
                    subject_type=TargetSubject.SALESPERSON, subject_id=sp.id,
                    metric=TargetMetric.REVENUE, period=TargetPeriod.MONTHLY,
                    period_start=pstart, period_end=pend,
                    target_value=money(rng.randint(900, 2400) * 1000),
                    currency="TRY", notes=DEMO_TAG,
                ))
            db.add(Target(
                subject_type=TargetSubject.COMPANY, subject_id=0,
                metric=TargetMetric.REVENUE, period=TargetPeriod.MONTHLY,
                period_start=pstart, period_end=pend,
                target_value=money(rng.randint(14, 22) * 1_000_000),
                currency="TRY", notes=DEMO_TAG,
            ))
        db.flush()
        stats["targets"] = len(sp_rows) * 3 + 3

        print(f"  master data ready: {stats}")

        # ------------------------------------------------------------------
        # Sales history
        # ------------------------------------------------------------------
        stats.update(
            _generate_history(
                db, rng,
                products=product_rows,
                customers=cust_rows,
                salespeople=sp_rows,
                vehicles=veh_rows,
                warehouses=wh_rows,
                months=months,
            )
        )

        # ------------------------------------------------------------------
        # Today's operating position
        # ------------------------------------------------------------------
        stats.update(
            _seed_today(
                db, rng,
                products=product_rows,
                salespeople=sp_rows,
                vehicles=veh_rows,
                warehouses=wh_rows,
                templates=route_rows,
            )
        )

    return stats


def _seed_today(
    db: Session,
    rng: random.Random,
    *,
    products: list[Product],
    salespeople: list[Salesperson],
    vehicles: list[Vehicle],
    warehouses: list[Warehouse],
    templates: list[Route],
) -> dict[str, int]:
    """
    Put the business into a believable "this morning" state.

    Without this the field screens open on an empty system: no route to drive,
    no stock on the van, nothing to sell.  Loads real stock from the depot into
    each van through the ledger, so van stock is as auditable as depot stock.
    """
    from app.core.enums import RouteStatus

    today = date.today()
    wd = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][today.weekday()]
    central = warehouses[0]
    moves: list[dict] = []
    routes_made = 0

    # --- Today's routes from the matching templates ---------------------------
    for tpl in templates:
        if tpl.weekday != wd:
            continue
        stops = db.execute(
            select(RouteStop).where(RouteStop.route_id == tpl.id).order_by(RouteStop.sequence)
        ).scalars().all()
        if not stops:
            continue
        live = Route(
            code=f"{tpl.code}-{today:%Y%m%d}",
            name=f"{tpl.name} ({today:%d.%m.%Y})",
            is_template=False,
            template_id=tpl.id,
            route_date=today,
            weekday=wd,
            salesperson_id=tpl.salesperson_id,
            vehicle_id=tpl.vehicle_id,
            region_id=tpl.region_id,
            start_warehouse_id=tpl.start_warehouse_id,
            end_warehouse_id=tpl.end_warehouse_id,
            status=RouteStatus.PLANNED,
            planned_stops=len(stops),
            planned_start_time="08:30",
            description=DEMO_TAG,
        )
        db.add(live)
        db.flush()
        for s in stops:
            db.add(RouteStop(
                route_id=live.id, customer_id=s.customer_id, sequence=s.sequence,
                service_time_minutes=s.service_time_minutes, is_priority=s.is_priority,
            ))
        routes_made += 1
    db.flush()

    # --- Load each van from the depot ----------------------------------------
    balances = {
        (b.warehouse_id, b.product_id, b.lot_id): b
        for b in db.execute(
            select(StockBalance).where(StockBalance.warehouse_id == central.id)
        ).scalars()
    }
    running = {k: Decimal(v.quantity) for k, v in balances.items()}
    van_totals: dict[tuple[int, int, int], Decimal] = defaultdict(lambda: Decimal("0"))
    at = datetime.combine(today, time(7, 0))

    for veh in vehicles:
        if not veh.warehouse_id:
            continue
        picks = rng.sample(products, k=min(28, len(products)))
        for p in picks:
            per_case = int(p.units_per_case or 1)
            cases = rng.randint(6, 40)
            need = Decimal(cases * per_case)
            key = next(
                (k for k in running if k[1] == p.id and running[k] >= need), None
            )
            if key is None:
                continue
            lot_id = key[2]
            running[key] -= need
            moves.append({
                "warehouse_id": central.id, "product_id": p.id, "lot_id": lot_id,
                "movement_type": StockMovementType.TRANSFER_OUT,
                "status": StockStatus.AVAILABLE, "quantity": -need,
                "unit_cost": p.cost_price, "total_cost": money(need * p.cost_price),
                "balance_after": qty(running[key]), "moved_at": at,
                "counterparty_warehouse_id": veh.warehouse_id,
                "reference_type": "VAN_LOAD", "notes": DEMO_TAG,
            })
            vkey = (veh.warehouse_id, p.id, lot_id)
            van_totals[vkey] += need
            moves.append({
                "warehouse_id": veh.warehouse_id, "product_id": p.id, "lot_id": lot_id,
                "movement_type": StockMovementType.VEHICLE_LOAD,
                "status": StockStatus.AVAILABLE, "quantity": need,
                "unit_cost": p.cost_price, "total_cost": money(need * p.cost_price),
                "balance_after": qty(van_totals[vkey]), "moved_at": at,
                "counterparty_warehouse_id": central.id,
                "reference_type": "VAN_LOAD", "notes": DEMO_TAG,
            })

    db.bulk_insert_mappings(StockMovement, moves)

    # --- Bugunun gun oturumlarini ac -------------------------------------
    # Arac yuklendi ve rota olusturuldu ama gun acilmazsa saha ekrani hicbir
    # plasiyerin gercekte bulunmayacagi bir durumda acilir: "bu kullaniciya
    # bagli acik arac yok". Sicak satis ekrani da katalog moduna duser.
    from app.services import day_session_service

    acilan = 0
    for sp in salespeople:
        if not sp.default_vehicle_id:
            continue
        rota = db.execute(
            select(Route).where(Route.salesperson_id == sp.id,
                                Route.route_date == today)
        ).scalars().first()
        try:
            day_session_service.open_day(
                db, salesperson_id=sp.id, vehicle_id=sp.default_vehicle_id,
                route_id=rota.id if rota else None,
                start_odometer=float(120000 + sp.id * 137),
                session_date=today, commit=False, notes=DEMO_TAG)
            acilan += 1
        except Exception:
            # Ayni gun icin oturum zaten varsa sorun degil.
            pass

    # Depot balances shrink by what left; van balances are new rows.
    for key, remaining in running.items():
        row = balances.get(key)
        if row is not None:
            row.quantity = qty(remaining)
    cost_by_product = {p.id: p.cost_price for p in products}
    db.bulk_insert_mappings(StockBalance, [
        {"warehouse_id": w, "product_id": p, "lot_id": lot,
         "status": StockStatus.AVAILABLE, "quantity": qty(total),
         "reserved_quantity": Decimal("0"),
         "average_cost": cost_by_product.get(p, Decimal("0")),
         "last_movement_at": utcnow(), "created_at": utcnow(), "updated_at": utcnow()}
        for (w, p, lot), total in van_totals.items()
    ])
    db.commit()

    # Re-assert the ledger invariant after the van loads.
    mv = db.execute(select(func.sum(StockMovement.quantity))).scalar_one() or 0
    bal = db.execute(select(func.sum(StockBalance.quantity))).scalar_one() or 0
    drift = abs(Decimal(str(mv)) - Decimal(str(bal)))
    print(f"  post-load ledger vs balances drift: {drift}")
    if drift > Decimal("0.001"):
        raise SystemExit(f"stock integrity check FAILED after van load (drift={drift})")

    return {"today_routes": routes_made, "van_load_movements": len(moves),
            "day_sessions_opened": acilan}


def _generate_history(
    db: Session,
    rng: random.Random,
    *,
    products: list[Product],
    customers: list[Customer],
    salespeople: list[Salesperson],
    vehicles: list[Vehicle],
    warehouses: list[Warehouse],
    months: int,
) -> dict[str, int]:
    """
    Simulate ``months`` of trading.

    Written with bulk inserts and a Python-side running balance rather than
    per-row service calls: a year of trading for 500 customers is ~100k rows,
    and going through the full posting path for each would take hours.  The
    resulting ledger is still exactly consistent — balances are rebuilt from
    the movements at the end and asserted.
    """
    today = date.today()
    start = today - timedelta(days=30 * months)
    central = warehouses[0]

    # --- Lots + opening stock -------------------------------------------------
    lots_by_product: dict[int, list[Lot]] = defaultdict(list)
    movements: list[dict] = []
    seq_time = datetime.combine(start - timedelta(days=1), time(8, 0))

    for p in products:
        shelf = p.shelf_life_days or 365
        # Two live lots per product so FEFO has something to choose between.
        for k in range(2):
            produced = today - timedelta(days=rng.randint(1, max(2, shelf // 3)))
            lot = Lot(
                product_id=p.id,
                lot_number=f"L{p.id:04d}-{k + 1}",
                production_date=produced,
                expiry_date=produced + timedelta(days=shelf),
                received_date=produced + timedelta(days=1),
                unit_cost=p.cost_price,
                supplier_name="Demo Üretim A.Ş.",
                notes=DEMO_TAG,
            )
            db.add(lot)
            lots_by_product[p.id].append(lot)
    db.flush()

    per_case = {p.id: int(p.units_per_case or 1) for p in products}
    # Inbound stock is generated *after* the simulation, sized from what the
    # year actually consumed.  A fixed opening figure either starves the
    # simulation (sales stop when the depot empties) or leaves an absurd pile
    # of stock; deriving it from real demand avoids both.
    consumed: dict[tuple[int, str], Decimal] = defaultdict(lambda: Decimal("0"))

    # --- Trading days ---------------------------------------------------------
    sales_rows: list[dict] = []
    sale_items: list[dict] = []
    invoice_rows: list[dict] = []
    invoice_items: list[dict] = []
    payment_rows: list[dict] = []
    ledger_rows: list[dict] = []
    visit_rows: list[dict] = []
    order_rows: list[dict] = []
    order_items: list[dict] = []

    sale_moves: list[dict] = []
    sale_no = inv_no = pay_no = ord_no = 0
    cust_stats: dict[int, dict] = defaultdict(
        lambda: {"count": 0, "total": Decimal("0"), "first": None, "last": None, "paid": Decimal("0")}
    )

    # A stable per-customer/product affinity gives products realistic,
    # repeatable buyers instead of uniform noise.
    affinity: dict[int, list[Product]] = {}
    for c in customers:
        k = rng.randint(4, 12)
        affinity[c.id] = rng.sample(products, k=min(k, len(products)))

    day = start
    while day <= today:
        wd = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][day.weekday()]
        if wd == "SUN":
            day += timedelta(days=1)
            continue

        wfactor = weekday_factor(day)
        for cust in customers:
            if cust.status != CustomerStatus.ACTIVE:
                continue
            if wd not in (cust.visit_days or "").split(","):
                continue

            sp = next((s for s in salespeople if s.id == cust.default_salesperson_id), salespeople[0])
            veh = next((v for v in vehicles if v.id == sp.default_vehicle_id), vehicles[0])

            visited = rng.random() < 0.92
            bought = visited and rng.random() < 0.78
            visit_rows.append({
                "visit_date": day, "customer_id": cust.id, "salesperson_id": sp.id,
                "vehicle_id": veh.id,
                "outcome": VisitOutcome.SALE if bought else (
                    VisitOutcome.NO_ORDER if visited else VisitOutcome.CLOSED
                ),
                "duration_minutes": rng.randint(5, 25),
                "latitude": cust.latitude, "longitude": cust.longitude,
                "is_in_geofence": True, "lines_count": 0,
                "sale_amount": Decimal("0"), "collected_amount": Decimal("0"),
                "return_amount": Decimal("0"),
                "created_at": utcnow(), "updated_at": utcnow(),
            })
            if not bought:
                continue

            basket = rng.sample(affinity[cust.id], k=min(len(affinity[cust.id]), rng.randint(2, 6)))
            gross = net = vat_total = cost_total = Decimal("0")
            moment = datetime.combine(day, time(rng.randint(8, 17), rng.randint(0, 59)))
            lines: list[dict] = []
            # Staged here so an empty basket cannot consume a document number.
            pending_moves: list[dict] = []
            month_key = f"{day.year}-{day.month:02d}"

            for p in basket:
                sf = seasonal_factor(day, p.storage_condition)
                cases = max(1, int(round(rng.uniform(1, 8) * sf * wfactor)))
                base_q = Decimal(cases * per_case[p.id])

                unit_price = p.sale_price
                line_gross = money(unit_price * base_q)
                disc_pct = rng.choice([0, 0, 0, 0, 2, 3, 5])
                disc = money(line_gross * Decimal(disc_pct) / Decimal(100))
                line_net = money(line_gross - disc)
                line_vat = money(line_net * Decimal(str(p.vat_rate)) / Decimal(100))
                line_cost = money(p.cost_price * base_q)

                gross += line_gross
                net += line_net
                vat_total += line_vat
                cost_total += line_cost

                lot = lots_by_product[p.id][0]
                consumed[(p.id, month_key)] += base_q
                pending_moves.append({
                    "warehouse_id": central.id, "product_id": p.id, "lot_id": lot.id,
                    "movement_type": StockMovementType.SALE, "status": StockStatus.AVAILABLE,
                    "quantity": -base_q, "unit_cost": p.cost_price,
                    "total_cost": line_cost, "balance_after": Decimal("0"),
                    "moved_at": moment, "reference_type": "SALE",
                    "customer_id": cust.id, "salesperson_id": sp.id, "notes": DEMO_TAG,
                })
                lines.append({
                    "product_id": p.id, "quantity": Decimal(cases), "uom": UnitOfMeasure.CASE,
                    "uom_factor": Decimal(per_case[p.id]), "base_quantity": base_q,
                    "unit_price": unit_price, "list_price": unit_price,
                    "gross_amount": line_gross, "discount_percent": float(disc_pct),
                    "discount_amount": disc, "net_amount": line_net,
                    "vat_rate": p.vat_rate, "vat_amount": line_vat,
                    "total_amount": money(line_net + line_vat),
                    "unit_cost": p.cost_price, "total_cost": line_cost,
                    "margin_amount": money(line_net - line_cost),
                    "lot_id": lot.id,
                })

            if not lines:
                continue

            # Only a basket that actually produced lines consumes numbers.
            sale_no += 1
            inv_no += 1
            ord_no += 1
            for mv in pending_moves:
                mv["reference_id"] = sale_no
            sale_moves.extend(pending_moves)

            total = money(net + vat_total)
            margin = money(net - cost_total)
            due = day + timedelta(days=cust.payment_term_days or 0)
            pays_now = cust.payment_method != PaymentMethod.OPEN_ACCOUNT or rng.random() < 0.55
            paid = total if pays_now else Decimal("0")

            order_rows.append({
                "order_no": f"SIP-{day.year}-{ord_no:06d}", "order_type": OrderType.HOT_SALE,
                "status": "DELIVERED", "order_date": day, "delivery_date": day,
                "ordered_at": moment, "customer_id": cust.id, "salesperson_id": sp.id,
                "vehicle_id": veh.id, "warehouse_id": central.id, "currency": "TRY",
                "gross_amount": gross, "net_amount": net, "vat_amount": vat_total,
                "total_amount": total, "total_cost": cost_total, "margin_amount": margin,
                "payment_method": cust.payment_method, "line_count": len(lines),
                "created_at": moment, "updated_at": moment,
            })
            order_items.extend({**ln, "order_no_ref": ord_no} for ln in lines)

            sales_rows.append({
                "sale_no": f"SAT-{day.year}-{sale_no:06d}", "sale_date": day, "sold_at": moment,
                "customer_id": cust.id, "salesperson_id": sp.id, "vehicle_id": veh.id,
                "warehouse_id": central.id, "is_hot_sale": True, "is_posted": True,
                "posted_at": moment, "currency": "TRY", "gross_amount": gross,
                "net_amount": net, "vat_amount": vat_total, "total_amount": total,
                "total_cost": cost_total, "margin_amount": margin, "paid_amount": paid,
                "due_amount": money(total - paid), "payment_method": cust.payment_method,
                "line_count": len(lines), "latitude": cust.latitude,
                "longitude": cust.longitude, "created_at": moment, "updated_at": moment,
                "notes": DEMO_TAG,
            })
            sale_items.extend({**ln, "sale_no_ref": sale_no} for ln in lines)

            invoice_rows.append({
                "invoice_no": f"FTR-{day.year}-{inv_no:06d}", "document_type": DocumentType.INVOICE,
                "status": InvoiceStatus.PAID if paid >= total else (
                    InvoiceStatus.OVERDUE if due < today else InvoiceStatus.ISSUED
                ),
                "sale_no_ref": sale_no, "customer_id": cust.id, "salesperson_id": sp.id,
                "invoice_date": day, "due_date": due, "issued_at": moment, "currency": "TRY",
                "net_amount": net, "vat_amount": vat_total, "total_amount": total,
                "paid_amount": paid, "open_amount": money(total - paid),
                "created_at": moment, "updated_at": moment,
            })
            invoice_items.extend({**ln, "invoice_no_ref": inv_no} for ln in lines)

            ledger_rows.append({
                "customer_id": cust.id, "entry_type": LedgerEntryType.INVOICE,
                "entry_date": day, "due_date": due, "debit": total, "credit": Decimal("0"),
                "open_amount": money(total - paid), "is_settled": paid >= total,
                "currency": "TRY", "reference_type": "INVOICE", "reference_id": inv_no,
                "salesperson_id": sp.id, "created_at": moment,
            })

            if paid > 0:
                pay_no += 1
                payment_rows.append({
                    "payment_no": f"THS-{day.year}-{pay_no:06d}", "customer_id": cust.id,
                    "salesperson_id": sp.id, "payment_date": day, "received_at": moment,
                    "payment_method": (
                        cust.payment_method
                        if cust.payment_method != PaymentMethod.OPEN_ACCOUNT
                        else PaymentMethod.CASH
                    ),
                    "status": PaymentStatus.CLEARED, "currency": "TRY", "amount": paid,
                    "allocated_amount": paid, "unallocated_amount": Decimal("0"),
                    "invoice_no_ref": inv_no, "created_at": moment, "updated_at": moment,
                })
                ledger_rows.append({
                    "customer_id": cust.id, "entry_type": LedgerEntryType.PAYMENT,
                    "entry_date": day, "debit": Decimal("0"), "credit": paid,
                    "open_amount": Decimal("0"), "is_settled": True, "currency": "TRY",
                    "reference_type": "PAYMENT", "reference_id": pay_no,
                    "salesperson_id": sp.id, "created_at": moment,
                })

            st = cust_stats[cust.id]
            st["count"] += 1
            st["total"] += total
            st["paid"] += paid
            st["first"] = st["first"] or day
            st["last"] = day

            visit_rows[-1]["sale_amount"] = total
            visit_rows[-1]["collected_amount"] = paid
            visit_rows[-1]["lines_count"] = len(lines)

        day += timedelta(days=1)

    # ------------------------------------------------------------------
    # Inbound replenishment, sized from what the year actually consumed
    # ------------------------------------------------------------------
    # A depot is restocked from the factory every month, not filled once and
    # left to drain.  Each month receives that month's demand plus a 40% buffer,
    # and the first receipt also covers the opening position — so stock is
    # always positive, the ledger shows realistic inbound flow, and the closing
    # position is a plausible ~40% of a month's sales.
    months_seen = sorted({mk for (_pid, mk) in consumed})
    inbound: list[dict] = []
    for p in products:
        lot = lots_by_product[p.id][0]
        for i, mk in enumerate(months_seen):
            need = consumed.get((p.id, mk), Decimal("0"))
            if need <= 0 and i > 0:
                continue
            buffer = Decimal("1.4")
            qty_in = (need * buffer).quantize(Decimal("1"))
            if i == 0:
                # Opening position: this month's demand plus the buffer again.
                qty_in += (need * Decimal("0.5")).quantize(Decimal("1"))
            # Whole cases only — nobody ships a third of a case.
            pc = Decimal(per_case[p.id])
            qty_in = (qty_in / pc).quantize(Decimal("1")) * pc
            if qty_in <= 0:
                qty_in = pc * Decimal("10")

            y, m = (int(x) for x in mk.split("-"))
            at = datetime.combine(date(y, m, 1), time(6, 0))
            if i == 0:
                at = seq_time
            inbound.append({
                "warehouse_id": central.id, "product_id": p.id, "lot_id": lot.id,
                "movement_type": (
                    StockMovementType.OPENING if i == 0 else StockMovementType.RECEIPT
                ),
                "status": StockStatus.AVAILABLE, "quantity": qty_in,
                "unit_cost": p.cost_price, "total_cost": money(qty_in * p.cost_price),
                "balance_after": Decimal("0"), "moved_at": at, "notes": DEMO_TAG,
            })

    # Merge and replay chronologically so balance_after is a true running total.
    movements = inbound + sale_moves
    movements.sort(key=lambda m: (m["moved_at"], 0 if m["quantity"] > 0 else 1))
    running: dict[tuple[int, int, int], Decimal] = defaultdict(lambda: Decimal("0"))
    negative_hits = 0
    for mv in movements:
        key3 = (mv["warehouse_id"], mv["product_id"], mv["lot_id"])
        running[key3] += mv["quantity"]
        if running[key3] < 0:
            negative_hits += 1
        mv["balance_after"] = qty(running[key3])

    if negative_hits:
        print(f"  WARNING: {negative_hits} movements would drive stock negative")

    # --- Bulk insert ----------------------------------------------------------
    print(
        f"  simulated {len(sales_rows)} sales over {months} months "
        f"({len(inbound)} inbound, {len(sale_moves)} outbound) — writing…"
    )

    db.bulk_insert_mappings(Visit, visit_rows)
    db.bulk_insert_mappings(StockMovement, movements)

    db.bulk_insert_mappings(Order, [
        {k: v for k, v in r.items() if k != "order_no_ref"} for r in order_rows
    ])
    db.flush()
    order_ids = list(db.execute(select(Order.id).order_by(Order.id)).scalars())
    db.bulk_insert_mappings(OrderItem, [
        {**{k: v for k, v in ln.items() if k not in ("order_no_ref", "lot_id", "total_cost", "margin_amount")},
         "order_id": order_ids[ln["order_no_ref"] - 1], "line_no": i % 20 + 1}
        for i, ln in enumerate(order_items)
    ])

    db.bulk_insert_mappings(Sale, [
        {k: v for k, v in r.items() if k != "sale_no_ref"} for r in sales_rows
    ])
    db.flush()
    sale_ids = list(db.execute(select(Sale.id).order_by(Sale.id)).scalars())
    db.bulk_insert_mappings(SaleItem, [
        {**{k: v for k, v in ln.items() if k != "sale_no_ref"},
         "sale_id": sale_ids[ln["sale_no_ref"] - 1], "line_no": i % 20 + 1}
        for i, ln in enumerate(sale_items)
    ])

    db.bulk_insert_mappings(Invoice, [
        {**{k: v for k, v in r.items() if k != "sale_no_ref"},
         "sale_id": sale_ids[r["sale_no_ref"] - 1]}
        for r in invoice_rows
    ])
    db.flush()
    invoice_ids = list(db.execute(select(Invoice.id).order_by(Invoice.id)).scalars())
    db.bulk_insert_mappings(InvoiceItem, [
        {"invoice_id": invoice_ids[ln["invoice_no_ref"] - 1], "product_id": ln["product_id"],
         "line_no": i % 20 + 1, "quantity": ln["quantity"], "uom": ln["uom"],
         "unit_price": ln["unit_price"], "discount_amount": ln["discount_amount"],
         "net_amount": ln["net_amount"], "vat_rate": ln["vat_rate"],
         "vat_amount": ln["vat_amount"], "total_amount": ln["total_amount"]}
        for i, ln in enumerate(invoice_items)
    ])

    db.bulk_insert_mappings(Payment, [
        {k: v for k, v in r.items() if k != "invoice_no_ref"} for r in payment_rows
    ])
    db.flush()
    payment_ids = list(db.execute(select(Payment.id).order_by(Payment.id)).scalars())
    db.bulk_insert_mappings(PaymentAllocation, [
        {"payment_id": payment_ids[i], "invoice_id": invoice_ids[r["invoice_no_ref"] - 1],
         "amount": r["amount"], "created_at": r["created_at"], "updated_at": r["created_at"]}
        for i, r in enumerate(payment_rows)
    ])

    # Ledger with a per-customer running balance.
    balances: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    ledger_rows.sort(key=lambda r: (r["customer_id"], r["entry_date"], r["entry_type"]))
    for row in ledger_rows:
        balances[row["customer_id"]] += row["debit"] - row["credit"]
        row["balance_after"] = money(balances[row["customer_id"]])
    db.bulk_insert_mappings(CustomerLedger, ledger_rows)

    # --- Materialise stock balances from the ledger ---------------------------
    db.flush()
    agg = db.execute(
        select(
            StockMovement.warehouse_id,
            StockMovement.product_id,
            StockMovement.lot_id,
            func.sum(StockMovement.quantity),
        ).group_by(
            StockMovement.warehouse_id, StockMovement.product_id, StockMovement.lot_id
        )
    ).all()
    cost_by_product = {p.id: p.cost_price for p in products}
    db.bulk_insert_mappings(StockBalance, [
        {"warehouse_id": w, "product_id": p, "lot_id": lot or 0,
         "status": StockStatus.AVAILABLE, "quantity": qty(total or 0),
         "reserved_quantity": Decimal("0"),
         "average_cost": cost_by_product.get(p, Decimal("0")),
         "last_movement_at": utcnow(),
         "created_at": utcnow(), "updated_at": utcnow()}
        for w, p, lot, total in agg
    ])

    # --- Denormalised customer figures ---------------------------------------
    for cust in customers:
        st = cust_stats.get(cust.id)
        if not st or not st["count"]:
            continue
        cust.order_count = st["count"]
        cust.total_sales_amount = money(st["total"])
        cust.total_paid_amount = money(st["paid"])
        cust.average_order_value = money(st["total"] / st["count"])
        cust.first_order_date = st["first"]
        cust.last_order_date = st["last"]
        cust.last_visit_date = st["last"]
        cust.balance = money(balances[cust.id])
        cust.overdue_balance = money(max(Decimal("0"), balances[cust.id]))

    db.commit()

    # --- Integrity assertion --------------------------------------------------
    mv_total = db.execute(select(func.sum(StockMovement.quantity))).scalar_one() or 0
    bal_total = db.execute(select(func.sum(StockBalance.quantity))).scalar_one() or 0
    drift = abs(Decimal(str(mv_total)) - Decimal(str(bal_total)))
    print(f"  stock ledger vs balances drift: {drift}")
    if drift > Decimal("0.001"):
        raise SystemExit(f"stock integrity check FAILED (drift={drift})")

    return {
        "visits": len(visit_rows),
        "orders": len(order_rows),
        "sales": len(sales_rows),
        "sale_items": len(sale_items),
        "invoices": len(invoice_rows),
        "payments": len(payment_rows),
        "ledger_entries": len(ledger_rows),
        "stock_movements": len(movements),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic demo data")
    ap.add_argument("--customers", type=int, default=500)
    ap.add_argument("--products", type=int, default=100)
    ap.add_argument("--salespeople", type=int, default=10)
    ap.add_argument("--warehouses", type=int, default=3)
    ap.add_argument("--routes", type=int, default=30)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--reset", action="store_true", help="wipe existing demo data first")
    args = ap.parse_args()

    print("Demo veri üretiliyor / Generating demo data…")
    stats = seed(
        customers=args.customers,
        products=args.products,
        salespeople=args.salespeople,
        warehouses=args.warehouses,
        routes=args.routes,
        months=args.months,
        reset=args.reset,
        seed_value=args.seed,
    )
    print("\nTamamlandı / Done:")
    for k, v in stats.items():
        print(f"  {k:20s} {v}")


if __name__ == "__main__":
    main()
