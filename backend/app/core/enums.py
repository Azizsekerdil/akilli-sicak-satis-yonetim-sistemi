"""
Domain vocabulary — every enumerated value used across the system.

Stored in the database as short uppercase strings (portable across SQLite and
PostgreSQL, and stable across migrations).  Human-readable labels live in the
i18n catalogues, never here.
"""

from __future__ import annotations

from enum import StrEnum


# ===========================================================================
# Identity & access
# ===========================================================================
class RoleCode(StrEnum):
    SYSTEM_ADMIN = "SYSTEM_ADMIN"
    COMPANY_OWNER = "COMPANY_OWNER"
    GENERAL_MANAGER = "GENERAL_MANAGER"
    SALES_MANAGER = "SALES_MANAGER"
    REGIONAL_SALES_MANAGER = "REGIONAL_SALES_MANAGER"
    FIELD_SALES_SUPERVISOR = "FIELD_SALES_SUPERVISOR"
    SALESPERSON = "SALESPERSON"          # Plasiyer
    DRIVER = "DRIVER"                    # Şoför
    MERCHANDISER = "MERCHANDISER"
    WAREHOUSE_MANAGER = "WAREHOUSE_MANAGER"
    WAREHOUSE_STAFF = "WAREHOUSE_STAFF"
    LOGISTICS_STAFF = "LOGISTICS_STAFF"
    ACCOUNTING = "ACCOUNTING"
    COLLECTION_STAFF = "COLLECTION_STAFF"
    MARKETING = "MARKETING"
    TRADE_MARKETING = "TRADE_MARKETING"
    SALES_ANALYST = "SALES_ANALYST"
    AI_MANAGER = "AI_MANAGER"
    AUDITOR = "AUDITOR"


class DataScope(StrEnum):
    """How much of the data a role may see."""

    ALL = "ALL"                # entire company
    REGION = "REGION"          # own region(s)
    TEAM = "TEAM"              # own team / supervised salespeople
    OWN = "OWN"                # only records they created / own
    NONE = "NONE"


class PermissionAction(StrEnum):
    VIEW = "VIEW"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    APPROVE = "APPROVE"
    EXPORT = "EXPORT"
    EXECUTE = "EXECUTE"


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    LOCKED = "LOCKED"
    SUSPENDED = "SUSPENDED"


# ===========================================================================
# Products
# ===========================================================================
class UnitOfMeasure(StrEnum):
    PIECE = "PIECE"        # adet
    CASE = "CASE"          # koli
    PACK = "PACK"          # paket
    PALLET = "PALLET"      # palet
    KILOGRAM = "KILOGRAM"
    GRAM = "GRAM"
    LITRE = "LITRE"
    MILLILITRE = "MILLILITRE"


class ProductStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PASSIVE = "PASSIVE"
    DISCONTINUED = "DISCONTINUED"


class StorageCondition(StrEnum):
    AMBIENT = "AMBIENT"
    CHILLED = "CHILLED"
    FROZEN = "FROZEN"


# ===========================================================================
# Warehouses, stock & movements
# ===========================================================================
class WarehouseType(StrEnum):
    CENTRAL = "CENTRAL"        # Merkez depo
    REGIONAL = "REGIONAL"      # Bölge deposu
    TRANSIT = "TRANSIT"        # Ara depo
    VEHICLE = "VEHICLE"        # Araç deposu
    QUARANTINE = "QUARANTINE"  # Karantina / hasarlı


class StockMovementType(StrEnum):
    RECEIPT = "RECEIPT"                    # giriş (üretim/tedarik)
    ISSUE = "ISSUE"                        # çıkış
    TRANSFER_OUT = "TRANSFER_OUT"
    TRANSFER_IN = "TRANSFER_IN"
    VEHICLE_LOAD = "VEHICLE_LOAD"          # araç yükleme
    VEHICLE_UNLOAD = "VEHICLE_UNLOAD"      # araç boşaltma
    SALE = "SALE"
    SALE_RETURN = "SALE_RETURN"            # müşteri iadesi
    COUNT_ADJUSTMENT = "COUNT_ADJUSTMENT"  # sayım düzeltme
    ADJUSTMENT = "ADJUSTMENT"              # manuel düzeltme
    WASTAGE = "WASTAGE"                    # fire
    DAMAGE = "DAMAGE"                      # hasarlı
    EXPIRY = "EXPIRY"                      # SKT geçmiş
    PROMOTION = "PROMOTION"                # bedelsiz / kampanya çıkışı
    OPENING = "OPENING"                    # açılış stoğu


#: Movement types that increase stock. Everything else decreases it.
INBOUND_MOVEMENTS: frozenset[str] = frozenset(
    {
        StockMovementType.RECEIPT,
        StockMovementType.TRANSFER_IN,
        StockMovementType.VEHICLE_LOAD,
        StockMovementType.SALE_RETURN,
        StockMovementType.OPENING,
    }
)


class StockStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    QUARANTINE = "QUARANTINE"
    DAMAGED = "DAMAGED"
    EXPIRED = "EXPIRED"


class AllocationStrategy(StrEnum):
    FEFO = "FEFO"   # First Expired, First Out — default for food & beverage
    FIFO = "FIFO"   # First In, First Out
    LIFO = "LIFO"


class CountStatus(StrEnum):
    DRAFT = "DRAFT"
    IN_PROGRESS = "IN_PROGRESS"
    COUNTED = "COUNTED"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"


class TransferStatus(StrEnum):
    DRAFT = "DRAFT"
    IN_TRANSIT = "IN_TRANSIT"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


# ===========================================================================
# Vehicles & field operations
# ===========================================================================
class VehicleType(StrEnum):
    VAN = "VAN"
    TRUCK = "TRUCK"
    PICKUP = "PICKUP"
    REFRIGERATED = "REFRIGERATED"
    MOTORCYCLE = "MOTORCYCLE"


class VehicleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    MAINTENANCE = "MAINTENANCE"
    INACTIVE = "INACTIVE"


class RouteStatus(StrEnum):
    PLANNED = "PLANNED"
    OPTIMIZED = "OPTIMIZED"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class StopStatus(StrEnum):
    PENDING = "PENDING"
    ARRIVED = "ARRIVED"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"       # atlanan müşteri
    FAILED = "FAILED"


class VisitOutcome(StrEnum):
    SALE = "SALE"
    NO_SALE = "NO_SALE"
    CLOSED = "CLOSED"          # kapalı
    NO_ORDER = "NO_ORDER"      # sipariş vermedi
    PAYMENT_ONLY = "PAYMENT_ONLY"
    RETURN_ONLY = "RETURN_ONLY"
    MERCHANDISING = "MERCHANDISING"


class DaySessionStatus(StrEnum):
    OPEN = "OPEN"
    RECONCILING = "RECONCILING"
    CLOSED = "CLOSED"
    DISPUTED = "DISPUTED"


# ===========================================================================
# Customers / CRM
# ===========================================================================
class CustomerType(StrEnum):
    GROCERY = "GROCERY"                # bakkal
    MARKET = "MARKET"
    SUPERMARKET = "SUPERMARKET"
    RESTAURANT = "RESTAURANT"
    CAFE = "CAFE"
    HOTEL = "HOTEL"
    KIOSK = "KIOSK"                    # büfe
    CANTEEN = "CANTEEN"                # kantin
    SCHOOL = "SCHOOL"
    HOSPITAL = "HOSPITAL"
    GAS_STATION = "GAS_STATION"
    WHOLESALER = "WHOLESALER"          # toptancı
    DEALER = "DEALER"                  # bayi
    DISTRIBUTOR = "DISTRIBUTOR"
    HORECA = "HORECA"
    OTHER = "OTHER"


class SalesChannel(StrEnum):
    TRADITIONAL = "TRADITIONAL"
    MODERN = "MODERN"
    HORECA = "HORECA"
    WHOLESALE = "WHOLESALE"
    ONLINE = "ONLINE"
    INSTITUTIONAL = "INSTITUTIONAL"


class CustomerStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PASSIVE = "PASSIVE"
    BLOCKED = "BLOCKED"        # risk/limit nedeniyle bloke
    PROSPECT = "PROSPECT"
    CHURNED = "CHURNED"


class VisitFrequency(StrEnum):
    DAILY = "DAILY"
    TWICE_WEEKLY = "TWICE_WEEKLY"
    WEEKLY = "WEEKLY"
    BIWEEKLY = "BIWEEKLY"
    MONTHLY = "MONTHLY"
    ON_DEMAND = "ON_DEMAND"


class Weekday(StrEnum):
    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"
    SAT = "SAT"
    SUN = "SUN"


# ===========================================================================
# Sales, invoicing, payments
# ===========================================================================
class OrderType(StrEnum):
    HOT_SALE = "HOT_SALE"        # sıcak satış — teslimat anında
    PRE_SALE = "PRE_SALE"        # ön satış — sonra teslim
    TELESALES = "TELESALES"
    SELF_SERVICE = "SELF_SERVICE"


class OrderStatus(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    PARTIALLY_DELIVERED = "PARTIALLY_DELIVERED"
    DELIVERED = "DELIVERED"
    INVOICED = "INVOICED"
    CANCELLED = "CANCELLED"


class DocumentType(StrEnum):
    INVOICE = "INVOICE"              # fatura
    WAYBILL = "WAYBILL"              # irsaliye
    CREDIT_NOTE = "CREDIT_NOTE"      # iade faturası
    RECEIPT = "RECEIPT"              # makbuz / fiş
    PROFORMA = "PROFORMA"


class InvoiceStatus(StrEnum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"


class PaymentMethod(StrEnum):
    CASH = "CASH"                    # nakit
    CREDIT_CARD = "CREDIT_CARD"
    BANK_TRANSFER = "BANK_TRANSFER"
    CHEQUE = "CHEQUE"                # çek
    PROMISSORY_NOTE = "PROMISSORY_NOTE"  # senet
    OPEN_ACCOUNT = "OPEN_ACCOUNT"    # açık hesap (vadeli)
    MIXED = "MIXED"                  # karma ödeme


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    CLEARED = "CLEARED"
    BOUNCED = "BOUNCED"      # karşılıksız çek
    CANCELLED = "CANCELLED"


class LedgerEntryType(StrEnum):
    """Customer current-account (cari hesap) entry types."""

    INVOICE = "INVOICE"              # borç
    PAYMENT = "PAYMENT"              # alacak
    CREDIT_NOTE = "CREDIT_NOTE"      # alacak (iade)
    DEBIT_NOTE = "DEBIT_NOTE"        # borç
    OPENING_BALANCE = "OPENING_BALANCE"
    ADJUSTMENT = "ADJUSTMENT"
    WRITE_OFF = "WRITE_OFF"


class ReturnReason(StrEnum):
    EXPIRED = "EXPIRED"              # SKT geçmiş
    DAMAGED = "DAMAGED"              # hasarlı
    WRONG_PRODUCT = "WRONG_PRODUCT"
    QUALITY = "QUALITY"
    OVERSTOCK = "OVERSTOCK"          # fazla stok
    CUSTOMER_REQUEST = "CUSTOMER_REQUEST"
    RECALL = "RECALL"
    OTHER = "OTHER"


class ReturnDisposition(StrEnum):
    RESALEABLE = "RESALEABLE"        # tekrar satılabilir -> stoğa döner
    SCRAP = "SCRAP"                  # imha / fire
    QUARANTINE = "QUARANTINE"


# ===========================================================================
# Campaigns & pricing
# ===========================================================================
class CampaignType(StrEnum):
    BUY_X_GET_Y = "BUY_X_GET_Y"                # 10 al 1 bedava
    QUANTITY_DISCOUNT = "QUANTITY_DISCOUNT"    # 5 koli -> %5
    VALUE_DISCOUNT = "VALUE_DISCOUNT"          # 20.000 TL üzeri -> %3
    BASKET_MIX = "BASKET_MIX"                  # 3 farklı ürün -> ekstra iskonto
    FIXED_PRICE = "FIXED_PRICE"                # özel fiyat
    PERCENT_DISCOUNT = "PERCENT_DISCOUNT"
    AMOUNT_DISCOUNT = "AMOUNT_DISCOUNT"
    FREE_GOODS = "FREE_GOODS"


class CampaignScope(StrEnum):
    ALL = "ALL"
    CUSTOMER = "CUSTOMER"
    CUSTOMER_TYPE = "CUSTOMER_TYPE"
    CHANNEL = "CHANNEL"
    REGION = "REGION"
    ROUTE = "ROUTE"
    SALESPERSON = "SALESPERSON"
    PRODUCT = "PRODUCT"
    CATEGORY = "CATEGORY"
    BRAND = "BRAND"


class CampaignStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class DiscountBasis(StrEnum):
    PERCENT = "PERCENT"
    AMOUNT = "AMOUNT"


# ===========================================================================
# Targets & analytics
# ===========================================================================
class TargetPeriod(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    YEARLY = "YEARLY"


class TargetSubject(StrEnum):
    COMPANY = "COMPANY"
    REGION = "REGION"
    ROUTE = "ROUTE"
    SALESPERSON = "SALESPERSON"
    PRODUCT = "PRODUCT"
    CATEGORY = "CATEGORY"
    BRAND = "BRAND"
    CUSTOMER = "CUSTOMER"


class TargetMetric(StrEnum):
    REVENUE = "REVENUE"
    VOLUME = "VOLUME"
    MARGIN = "MARGIN"
    COLLECTION = "COLLECTION"
    VISITS = "VISITS"
    NEW_CUSTOMERS = "NEW_CUSTOMERS"


class ForecastMethod(StrEnum):
    MOVING_AVERAGE = "MOVING_AVERAGE"
    EWMA = "EWMA"
    HOLT_WINTERS = "HOLT_WINTERS"
    SEASONAL_NAIVE = "SEASONAL_NAIVE"
    CROSTON = "CROSTON"                # intermittent demand
    LINEAR_TREND = "LINEAR_TREND"
    ENSEMBLE = "ENSEMBLE"


class AnomalyType(StrEnum):
    SALES_SPIKE = "SALES_SPIKE"
    SALES_DROP = "SALES_DROP"
    UNUSUAL_DISCOUNT = "UNUSUAL_DISCOUNT"
    UNUSUAL_RETURN = "UNUSUAL_RETURN"
    STOCK_VARIANCE = "STOCK_VARIANCE"
    COLLECTION_ANOMALY = "COLLECTION_ANOMALY"
    ROUTE_DEVIATION = "ROUTE_DEVIATION"
    PRICE_ANOMALY = "PRICE_ANOMALY"


class AnomalySeverity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ===========================================================================
# AI
# ===========================================================================
class AIProvider(StrEnum):
    LMSTUDIO = "LMSTUDIO"
    NVIDIA = "NVIDIA"
    CLAUDE = "CLAUDE"


class AITaskType(StrEnum):
    GENERAL = "GENERAL"
    ANALYSIS = "ANALYSIS"
    VISION = "VISION"
    MATH = "MATH"
    CODING = "CODING"
    REPORTING = "REPORTING"
    LONG_CONTEXT = "LONG_CONTEXT"
    EMBEDDING = "EMBEDDING"
    SQL = "SQL"


class AIAgentKind(StrEnum):
    ORCHESTRATOR = "ORCHESTRATOR"
    SALES = "SALES"
    FORECAST = "FORECAST"
    ROUTE = "ROUTE"
    INVENTORY = "INVENTORY"
    COLLECTION_RISK = "COLLECTION_RISK"
    REPORTING = "REPORTING"
    DATA_ANALYST = "DATA_ANALYST"
    CODING = "CODING"
    TESTING = "TESTING"
    DOCUMENTATION = "DOCUMENTATION"
    SECURITY = "SECURITY"


class AIRequestStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"


class AIPermissionLevel(StrEnum):
    """Escalating capability tiers for the in-app AI development terminal."""

    READ_ONLY = "READ_ONLY"
    PROJECT_WRITE = "PROJECT_WRITE"
    RUN_TESTS = "RUN_TESTS"
    PACKAGE_INSTALL = "PACKAGE_INSTALL"
    GIT_OPERATIONS = "GIT_OPERATIONS"
    SYSTEM_COMMAND = "SYSTEM_COMMAND"


#: Ordered from least to most privileged — used for tier comparisons.
AI_PERMISSION_ORDER: tuple[str, ...] = (
    AIPermissionLevel.READ_ONLY,
    AIPermissionLevel.PROJECT_WRITE,
    AIPermissionLevel.RUN_TESTS,
    AIPermissionLevel.PACKAGE_INSTALL,
    AIPermissionLevel.GIT_OPERATIONS,
    AIPermissionLevel.SYSTEM_COMMAND,
)


# ===========================================================================
# System
# ===========================================================================
class AuditAction(StrEnum):
    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    CANCEL = "CANCEL"
    SALE = "SALE"
    PRICE_CHANGE = "PRICE_CHANGE"
    DISCOUNT_APPLIED = "DISCOUNT_APPLIED"
    STOCK_ADJUSTMENT = "STOCK_ADJUSTMENT"
    STOCK_VARIANCE = "STOCK_VARIANCE"
    PAYMENT = "PAYMENT"
    PERMISSION_CHANGE = "PERMISSION_CHANGE"
    AI_ACTION = "AI_ACTION"
    BACKUP = "BACKUP"
    RESTORE = "RESTORE"
    EXPORT = "EXPORT"
    SETTING_CHANGE = "SETTING_CHANGE"


class NotificationType(StrEnum):
    LOW_STOCK = "LOW_STOCK"
    EXPIRY_WARNING = "EXPIRY_WARNING"
    OVERDUE_PAYMENT = "OVERDUE_PAYMENT"
    ROUTE_DELAY = "ROUTE_DELAY"
    STOCK_VARIANCE = "STOCK_VARIANCE"
    TARGET_RISK = "TARGET_RISK"
    BACKUP_FAILED = "BACKUP_FAILED"
    AI_SERVICE_ERROR = "AI_SERVICE_ERROR"
    API_QUOTA = "API_QUOTA"
    HIGH_AI_COST = "HIGH_AI_COST"
    CREDIT_LIMIT = "CREDIT_LIMIT"
    ANOMALY = "ANOMALY"
    SYSTEM = "SYSTEM"


class NotificationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class BackupType(StrEnum):
    FULL = "FULL"
    INCREMENTAL = "INCREMENTAL"
    DATABASE = "DATABASE"
    FILES = "FILES"
    SETTINGS = "SETTINGS"


class BackupStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    VERIFIED = "VERIFIED"
    CORRUPT = "CORRUPT"
    RESTORED = "RESTORED"


class HealthState(StrEnum):
    OK = "OK"
    WARNING = "WARNING"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class Language(StrEnum):
    TR = "tr"
    EN = "en"
