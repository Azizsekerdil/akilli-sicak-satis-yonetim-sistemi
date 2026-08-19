"""
Permission catalogue and the role → permission matrix.

Permissions are ``<resource>:<ACTION>`` pairs, e.g. ``sales.hot_sale:CREATE``.
The catalogue below is the single source of truth: it seeds the ``permissions``
table, drives the UI's menu visibility, and is what the API dependency checks.

Three enforcement layers:
    1. **Screen**  — can this role open the screen at all?
    2. **Action**  — VIEW / CREATE / UPDATE / DELETE / APPROVE / EXPORT / EXECUTE
    3. **Data scope** — ALL / REGION / TEAM / OWN — how much data is visible
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.enums import DataScope, PermissionAction, RoleCode

A = PermissionAction


@dataclass(frozen=True)
class ResourceDef:
    """One protected screen/resource and the actions it supports."""

    key: str
    module: str
    name_tr: str
    name_en: str
    actions: tuple[str, ...] = (A.VIEW,)
    sensitive: bool = False


# ===========================================================================
# Catalogue
# ===========================================================================
CRUD = (A.VIEW, A.CREATE, A.UPDATE, A.DELETE)
CRUDX = (A.VIEW, A.CREATE, A.UPDATE, A.DELETE, A.EXPORT)
VIEW_EXPORT = (A.VIEW, A.EXPORT)

RESOURCES: tuple[ResourceDef, ...] = (
    # --- Dashboard -------------------------------------------------------
    ResourceDef("dashboard.main", "dashboard", "Kontrol Paneli", "Dashboard", (A.VIEW,)),
    ResourceDef("dashboard.financial", "dashboard", "Finansal Özet", "Financial Summary", (A.VIEW,), True),

    # --- Sales -----------------------------------------------------------
    ResourceDef("sales.hot_sale", "sales", "Sıcak Satış", "Hot Sale", (A.VIEW, A.CREATE, A.EXECUTE)),
    ResourceDef("sales.orders", "sales", "Siparişler", "Orders", CRUDX + (A.APPROVE,)),
    ResourceDef("sales.sales", "sales", "Satışlar", "Sales", CRUDX + (A.APPROVE,)),
    ResourceDef("sales.invoices", "sales", "Faturalar", "Invoices", CRUDX),
    ResourceDef("sales.payments", "sales", "Tahsilatlar", "Collections", CRUDX),
    ResourceDef("sales.returns", "sales", "İadeler", "Returns", CRUDX + (A.APPROVE,)),
    ResourceDef("sales.discount_override", "sales", "İskonto Yetkisi", "Discount Override", (A.EXECUTE,), True),
    ResourceDef("sales.price_override", "sales", "Fiyat Değiştirme", "Price Override", (A.EXECUTE,), True),

    # --- Field -----------------------------------------------------------
    ResourceDef("field.salespersons", "field", "Plasiyerler", "Salespeople", CRUDX),
    ResourceDef("field.routes", "field", "Rotalar", "Routes", CRUDX + (A.EXECUTE,)),
    ResourceDef("field.map", "field", "Harita", "Map", (A.VIEW,)),
    ResourceDef("field.visits", "field", "Ziyaretler", "Visits", CRUDX),
    ResourceDef("field.vehicles", "field", "Araçlar", "Vehicles", CRUDX),
    ResourceDef("field.day_session", "field", "Gün Yönetimi", "Day Session", (A.VIEW, A.CREATE, A.UPDATE, A.APPROVE)),

    # --- Stock -----------------------------------------------------------
    ResourceDef("stock.products", "stock", "Ürünler", "Products", CRUDX),
    ResourceDef("stock.warehouses", "stock", "Depolar", "Warehouses", CRUDX),
    ResourceDef("stock.vehicle_stock", "stock", "Araç Stokları", "Vehicle Stock", VIEW_EXPORT),
    ResourceDef("stock.transfers", "stock", "Transferler", "Transfers", CRUDX + (A.APPROVE,)),
    ResourceDef("stock.counts", "stock", "Sayımlar", "Stock Counts", CRUDX + (A.APPROVE,)),
    ResourceDef("stock.adjustments", "stock", "Stok Düzeltme", "Stock Adjustments", (A.VIEW, A.CREATE, A.APPROVE), True),
    ResourceDef("stock.van_load", "stock", "Araç Yükleme", "Van Loading", (A.VIEW, A.CREATE, A.UPDATE, A.EXECUTE)),
    ResourceDef("stock.lots", "stock", "Lot / SKT", "Lots & Expiry", CRUDX),

    # --- CRM -------------------------------------------------------------
    ResourceDef("crm.customers", "crm", "Müşteriler", "Customers", CRUDX),
    ResourceDef("crm.ledger", "crm", "Cari Hesap", "Current Account", VIEW_EXPORT),
    ResourceDef("crm.risk", "crm", "Risk Analizi", "Risk Analysis", VIEW_EXPORT, True),
    ResourceDef("crm.credit_limit", "crm", "Kredi Limiti", "Credit Limit", (A.VIEW, A.UPDATE), True),

    # --- Marketing -------------------------------------------------------
    ResourceDef("marketing.campaigns", "marketing", "Kampanyalar", "Campaigns", CRUDX + (A.APPROVE,)),
    ResourceDef("marketing.price_lists", "marketing", "Fiyat Listeleri", "Price Lists", CRUDX),
    ResourceDef("marketing.discounts", "marketing", "İskontolar", "Discounts", CRUDX),

    # --- Analytics -------------------------------------------------------
    ResourceDef("analytics.reports", "analytics", "Raporlar", "Reports", VIEW_EXPORT),
    ResourceDef("analytics.statistics", "analytics", "İstatistik", "Statistics", VIEW_EXPORT),
    ResourceDef("analytics.forecasts", "analytics", "Tahminler", "Forecasts", (A.VIEW, A.EXECUTE, A.EXPORT)),
    ResourceDef("analytics.targets", "analytics", "Hedefler", "Targets", CRUDX),
    ResourceDef("analytics.anomalies", "analytics", "Anomaliler", "Anomalies", (A.VIEW, A.UPDATE, A.EXPORT)),

    # --- AI --------------------------------------------------------------
    ResourceDef("ai.copilot", "ai", "AI Satış Müdürü", "AI Sales Manager", (A.VIEW, A.EXECUTE)),
    ResourceDef("ai.assistant", "ai", "AI Plasiyer Asistanı", "AI Sales Assistant", (A.VIEW, A.EXECUTE)),
    ResourceDef("ai.terminal", "ai", "AI Terminal", "AI Terminal", (A.VIEW, A.EXECUTE), True),
    ResourceDef("ai.providers", "ai", "AI Sağlayıcıları", "AI Providers", (A.VIEW, A.UPDATE, A.EXECUTE), True),
    ResourceDef("ai.usage", "ai", "Token / Maliyet", "Token & Cost", VIEW_EXPORT),

    # --- Compliance & Human Sovereignty ----------------------------------
    # Ayri bir modul olmasinin sebebi: uyumluluk kanitina erisim, ticari
    # veriye erisimle ayni sey degildir. Bir DPO, musteri bakiyesini hic
    # gormeden isini yapabilmelidir.
    ResourceDef("compliance.overview", "compliance", "Uyumluluk Durumu",
                "Compliance Overview", (A.VIEW,)),
    ResourceDef("compliance.inventory", "compliance", "Veri Envanteri",
                "Data Inventory", (A.VIEW, A.CREATE, A.UPDATE, A.EXECUTE, A.EXPORT)),
    ResourceDef("compliance.consent", "compliance", "Rıza ve Aydınlatma",
                "Consent & Notices", CRUDX),
    ResourceDef("compliance.dsr", "compliance", "İlgili Kişi Başvuruları",
                "Data Subject Requests", CRUDX + (A.APPROVE,)),
    ResourceDef("compliance.transfers", "compliance", "Yurt Dışı Aktarım",
                "Cross-Border Transfers", CRUDX, True),
    ResourceDef("compliance.rulepacks", "compliance", "Mevzuat Paketleri",
                "Rule Packs", (A.VIEW, A.CREATE, A.UPDATE, A.APPROVE, A.EXECUTE), True),
    ResourceDef("compliance.evidence", "compliance", "Kanıt Kayıtları",
                "Evidence Records", VIEW_EXPORT, True),
    ResourceDef("hsp.policies", "compliance", "Hak Politikaları",
                "Rights Policies", CRUD + (A.APPROVE,), True),
    ResourceDef("hsp.receipts", "compliance", "Hak Makbuzları",
                "Rights Receipts", VIEW_EXPORT),
    ResourceDef("hsp.evaluate", "compliance", "Yetki Değerlendirme",
                "Authority Evaluation", (A.EXECUTE,)),

    # --- System ----------------------------------------------------------
    ResourceDef("system.users", "system", "Kullanıcılar", "Users", CRUD, True),
    ResourceDef("system.roles", "system", "Roller", "Roles", CRUD, True),
    ResourceDef("system.backup", "system", "Yedekleme", "Backup", (A.VIEW, A.CREATE, A.EXECUTE), True),
    ResourceDef("system.audit", "system", "Denetim Kaydı", "Audit Log", VIEW_EXPORT, True),
    ResourceDef("system.training", "system", "Eğitim Merkezi", "Training Centre", (A.VIEW, A.UPDATE)),
    ResourceDef("system.settings", "system", "Ayarlar", "Settings", (A.VIEW, A.UPDATE), True),
    ResourceDef("system.health", "system", "Sistem Sağlığı", "System Health", (A.VIEW,)),
    ResourceDef("system.notifications", "system", "Bildirimler", "Notifications", (A.VIEW, A.UPDATE)),
)

RESOURCE_BY_KEY: dict[str, ResourceDef] = {r.key: r for r in RESOURCES}


def permission_code(resource: str, action: str) -> str:
    return f"{resource}:{action}"


def all_permission_codes() -> list[str]:
    return [permission_code(r.key, a) for r in RESOURCES for a in r.actions]


# ===========================================================================
# Role definitions
# ===========================================================================
@dataclass(frozen=True)
class RoleDef:
    code: str
    name_tr: str
    name_en: str
    rank: int
    scope: str
    #: "*" grants everything.  Otherwise: resource keys (all their actions) or
    #: explicit "resource:ACTION" codes.
    grants: tuple[str, ...] = field(default_factory=tuple)


def _keys(module: str) -> tuple[str, ...]:
    return tuple(r.key for r in RESOURCES if r.module == module)


_READONLY_ALL = tuple(f"{r.key}:{A.VIEW}" for r in RESOURCES if not r.sensitive)

ROLES: tuple[RoleDef, ...] = (
    RoleDef(RoleCode.SYSTEM_ADMIN, "Sistem Yöneticisi", "System Administrator", 0, DataScope.ALL, ("*",)),
    RoleDef(RoleCode.COMPANY_OWNER, "Şirket Sahibi", "Company Owner", 5, DataScope.ALL, ("*",)),
    RoleDef(
        RoleCode.GENERAL_MANAGER, "Genel Müdür", "General Manager", 10, DataScope.ALL,
        _keys("dashboard") + _keys("sales") + _keys("field") + _keys("stock")
        + _keys("crm") + _keys("marketing") + _keys("analytics") + _keys("ai")
        + ("system.audit:VIEW", "system.notifications:VIEW", "system.health:VIEW",
           "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.SALES_MANAGER, "Satış Müdürü", "Sales Manager", 20, DataScope.ALL,
        _keys("dashboard") + _keys("sales") + _keys("field") + _keys("crm")
        + _keys("marketing") + _keys("analytics")
        + ("stock.products:VIEW", "stock.vehicle_stock:VIEW", "stock.van_load:VIEW",
           "stock.warehouses:VIEW", "ai.copilot:VIEW", "ai.copilot:EXECUTE",
           "ai.assistant:VIEW", "ai.assistant:EXECUTE", "ai.usage:VIEW",
           "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.REGIONAL_SALES_MANAGER, "Bölge Satış Müdürü", "Regional Sales Manager", 30, DataScope.REGION,
        _keys("dashboard") + _keys("sales") + _keys("field") + _keys("crm")
        + ("marketing.campaigns:VIEW", "marketing.price_lists:VIEW",
           "analytics.reports:VIEW", "analytics.reports:EXPORT",
           "analytics.statistics:VIEW", "analytics.targets:VIEW",
           "analytics.forecasts:VIEW", "analytics.anomalies:VIEW",
           "stock.products:VIEW", "stock.vehicle_stock:VIEW", "stock.van_load:VIEW",
           "stock.van_load:CREATE", "stock.van_load:EXECUTE",
           "ai.copilot:VIEW", "ai.copilot:EXECUTE", "ai.assistant:VIEW",
           "ai.assistant:EXECUTE", "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.FIELD_SALES_SUPERVISOR, "Saha Satış Şefi", "Field Sales Supervisor", 40, DataScope.TEAM,
        ("dashboard.main:VIEW",)
        + _keys("field")
        + ("sales.hot_sale:VIEW", "sales.orders:VIEW", "sales.orders:CREATE",
           "sales.orders:UPDATE", "sales.sales:VIEW", "sales.invoices:VIEW",
           "sales.payments:VIEW", "sales.payments:CREATE", "sales.returns:VIEW",
           "sales.returns:CREATE", "sales.returns:APPROVE",
           "crm.customers:VIEW", "crm.customers:CREATE", "crm.customers:UPDATE",
           "crm.ledger:VIEW", "stock.vehicle_stock:VIEW", "stock.van_load:VIEW",
           "stock.van_load:CREATE", "stock.van_load:EXECUTE", "stock.counts:VIEW",
           "stock.counts:CREATE", "stock.counts:APPROVE", "stock.products:VIEW",
           "analytics.reports:VIEW", "analytics.targets:VIEW",
           "ai.assistant:VIEW", "ai.assistant:EXECUTE",
           "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.SALESPERSON, "Plasiyer", "Salesperson", 60, DataScope.OWN,
        ("dashboard.main:VIEW",
         "sales.hot_sale:VIEW", "sales.hot_sale:CREATE", "sales.hot_sale:EXECUTE",
         "sales.orders:VIEW", "sales.orders:CREATE", "sales.orders:UPDATE",
         "sales.sales:VIEW", "sales.sales:CREATE",
         "sales.invoices:VIEW", "sales.invoices:CREATE",
         "sales.payments:VIEW", "sales.payments:CREATE",
         "sales.returns:VIEW", "sales.returns:CREATE",
         "field.routes:VIEW", "field.routes:EXECUTE", "field.visits:VIEW",
         "field.visits:CREATE", "field.visits:UPDATE", "field.map:VIEW",
         "field.day_session:VIEW", "field.day_session:CREATE", "field.day_session:UPDATE",
         "crm.customers:VIEW", "crm.customers:CREATE", "crm.customers:UPDATE",
         "crm.ledger:VIEW",
         "stock.vehicle_stock:VIEW", "stock.products:VIEW", "stock.lots:VIEW",
         "stock.counts:VIEW", "stock.counts:CREATE", "stock.van_load:VIEW",
         "marketing.campaigns:VIEW",
         "ai.assistant:VIEW", "ai.assistant:EXECUTE",
         "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.DRIVER, "Şoför", "Driver", 65, DataScope.OWN,
        ("dashboard.main:VIEW", "field.routes:VIEW", "field.map:VIEW",
         "field.visits:VIEW", "field.day_session:VIEW",
         "stock.vehicle_stock:VIEW", "stock.van_load:VIEW",
         "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.MERCHANDISER, "Merchandiser", "Merchandiser", 65, DataScope.OWN,
        ("dashboard.main:VIEW", "field.visits:VIEW", "field.visits:CREATE",
         "field.visits:UPDATE", "field.routes:VIEW", "field.map:VIEW",
         "crm.customers:VIEW", "crm.customers:UPDATE",
         "stock.products:VIEW", "marketing.campaigns:VIEW",
         "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.WAREHOUSE_MANAGER, "Depo Müdürü", "Warehouse Manager", 40, DataScope.ALL,
        ("dashboard.main:VIEW",) + _keys("stock")
        + ("field.vehicles:VIEW", "field.salespersons:VIEW",
           "analytics.reports:VIEW", "analytics.reports:EXPORT",
           "sales.orders:VIEW", "sales.returns:VIEW", "sales.returns:APPROVE",
           "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.WAREHOUSE_STAFF, "Depo Personeli", "Warehouse Staff", 70, DataScope.OWN,
        ("dashboard.main:VIEW",
         "stock.products:VIEW", "stock.warehouses:VIEW", "stock.vehicle_stock:VIEW",
         "stock.transfers:VIEW", "stock.transfers:CREATE", "stock.transfers:UPDATE",
         "stock.counts:VIEW", "stock.counts:CREATE", "stock.counts:UPDATE",
         "stock.van_load:VIEW", "stock.van_load:CREATE", "stock.van_load:EXECUTE",
         "stock.lots:VIEW", "stock.lots:CREATE",
         "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.LOGISTICS_STAFF, "Lojistik Personeli", "Logistics Staff", 70, DataScope.ALL,
        ("dashboard.main:VIEW", "field.routes:VIEW", "field.routes:UPDATE",
         "field.routes:EXECUTE", "field.vehicles:VIEW", "field.vehicles:UPDATE",
         "field.map:VIEW", "stock.transfers:VIEW", "stock.transfers:CREATE",
         "stock.vehicle_stock:VIEW", "analytics.reports:VIEW",
         "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.ACCOUNTING, "Muhasebe", "Accounting", 35, DataScope.ALL,
        ("dashboard.main:VIEW", "dashboard.financial:VIEW",
         "sales.invoices:VIEW", "sales.invoices:CREATE", "sales.invoices:UPDATE",
         "sales.invoices:EXPORT", "sales.payments:VIEW", "sales.payments:CREATE",
         "sales.payments:UPDATE", "sales.payments:EXPORT",
         "sales.returns:VIEW", "sales.sales:VIEW", "sales.sales:EXPORT",
         "crm.customers:VIEW", "crm.ledger:VIEW", "crm.ledger:EXPORT",
         "crm.risk:VIEW", "crm.risk:EXPORT", "crm.credit_limit:VIEW",
         "crm.credit_limit:UPDATE",
         "analytics.reports:VIEW", "analytics.reports:EXPORT",
         "analytics.statistics:VIEW", "system.audit:VIEW",
         "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.COLLECTION_STAFF, "Tahsilat Personeli", "Collection Staff", 55, DataScope.ALL,
        ("dashboard.main:VIEW",
         "sales.payments:VIEW", "sales.payments:CREATE", "sales.payments:UPDATE",
         "sales.payments:EXPORT", "sales.invoices:VIEW",
         "crm.customers:VIEW", "crm.ledger:VIEW", "crm.ledger:EXPORT",
         "crm.risk:VIEW", "analytics.reports:VIEW",
         "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.MARKETING, "Pazarlama", "Marketing", 45, DataScope.ALL,
        ("dashboard.main:VIEW",) + _keys("marketing")
        + ("crm.customers:VIEW", "stock.products:VIEW",
           "analytics.reports:VIEW", "analytics.statistics:VIEW",
           "analytics.forecasts:VIEW", "sales.sales:VIEW",
           "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.TRADE_MARKETING, "Trade Marketing", "Trade Marketing", 45, DataScope.ALL,
        ("dashboard.main:VIEW",) + _keys("marketing")
        + ("crm.customers:VIEW", "stock.products:VIEW", "field.visits:VIEW",
           "analytics.reports:VIEW", "analytics.statistics:VIEW",
           "sales.sales:VIEW", "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.SALES_ANALYST, "Satış Analisti", "Sales Analyst", 45, DataScope.ALL,
        ("dashboard.main:VIEW", "dashboard.financial:VIEW")
        + _keys("analytics")
        + ("sales.sales:VIEW", "sales.sales:EXPORT", "sales.orders:VIEW",
           "crm.customers:VIEW", "crm.risk:VIEW", "stock.products:VIEW",
           "field.routes:VIEW", "field.salespersons:VIEW",
           "marketing.campaigns:VIEW", "ai.copilot:VIEW", "ai.copilot:EXECUTE",
           "ai.usage:VIEW", "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.AI_MANAGER, "AI Yöneticisi", "AI Manager", 25, DataScope.ALL,
        ("dashboard.main:VIEW",) + _keys("ai")
        + ("analytics.reports:VIEW", "analytics.statistics:VIEW",
           "analytics.forecasts:VIEW", "analytics.forecasts:EXECUTE",
           "analytics.anomalies:VIEW", "system.settings:VIEW",
           "system.health:VIEW", "system.audit:VIEW",
           "system.notifications:VIEW", "system.training:VIEW"),
    ),
    RoleDef(
        RoleCode.AUDITOR, "Denetçi", "Auditor", 15, DataScope.ALL,
        _READONLY_ALL
        + ("system.audit:VIEW", "system.audit:EXPORT", "dashboard.financial:VIEW",
           "crm.risk:VIEW", "system.health:VIEW",
           "compliance.overview:VIEW", "compliance.inventory:VIEW",
           "compliance.inventory:EXPORT", "compliance.consent:VIEW",
           "compliance.dsr:VIEW", "compliance.transfers:VIEW",
           "compliance.rulepacks:VIEW", "compliance.evidence:VIEW",
           "compliance.evidence:EXPORT", "hsp.policies:VIEW",
           "hsp.receipts:VIEW", "hsp.receipts:EXPORT"),
    ),
)

ROLE_BY_CODE: dict[str, RoleDef] = {r.code: r for r in ROLES}


def expand_grants(grants: tuple[str, ...]) -> set[str]:
    """
    Turn a role's grant list into concrete ``resource:ACTION`` codes.

    ``"*"``            -> every permission
    ``"stock.products"`` -> every action on that resource
    ``"stock.products:VIEW"`` -> exactly that
    """
    out: set[str] = set()
    for g in grants:
        if g == "*":
            return set(all_permission_codes())
        if ":" in g:
            res, act = g.split(":", 1)
            if res in RESOURCE_BY_KEY and act in RESOURCE_BY_KEY[res].actions:
                out.add(g)
        elif g in RESOURCE_BY_KEY:
            out.update(permission_code(g, a) for a in RESOURCE_BY_KEY[g].actions)
    return out


def role_permissions(role_code: str) -> set[str]:
    role = ROLE_BY_CODE.get(role_code)
    return expand_grants(role.grants) if role else set()


def role_scope(role_code: str) -> str:
    role = ROLE_BY_CODE.get(role_code)
    return role.scope if role else DataScope.NONE


def accessible_modules(role_code: str) -> set[str]:
    """Modules the role can reach — used to build the sidebar."""
    perms = role_permissions(role_code)
    return {
        RESOURCE_BY_KEY[c.split(":")[0]].module
        for c in perms
        if c.split(":")[0] in RESOURCE_BY_KEY
    }
