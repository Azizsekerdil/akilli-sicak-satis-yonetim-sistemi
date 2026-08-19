"""
ORM model registry.

Importing this package registers every mapper against ``Base.metadata``, which
is what ``create_all()`` and Alembic autogenerate rely on.  Import order is
dependency-first so the schema reads top-down.
"""

from __future__ import annotations

from app.core.db import Base

from app.models.organization import Branch, Company, Region
from app.models.auth import (
    LoginAttempt,
    Permission,
    Role,
    RolePermission,
    User,
    UserSession,
)
from app.models.product import (
    Barcode,
    Brand,
    PriceList,
    PriceListItem,
    Product,
    ProductCategory,
    ProductUnit,
)
from app.models.warehouse import (
    Lot,
    StockBalance,
    StockCount,
    StockCountItem,
    StockMovement,
    StockTransfer,
    StockTransferItem,
    Warehouse,
)
from app.models.vehicle import DaySession, Salesperson, VanLoad, VanLoadItem, Vehicle
from app.models.customer import Customer, CustomerContact, CustomerLedger, CustomerNote
from app.models.route import GpsEvent, Route, RouteStop, Visit
from app.models.sales import (
    Invoice,
    InvoiceItem,
    Order,
    OrderItem,
    Payment,
    PaymentAllocation,
    ReturnDocument,
    ReturnItem,
    Sale,
    SaleItem,
)
from app.models.campaign import (
    Campaign,
    CampaignApplication,
    CampaignCondition,
    Discount,
)
from app.models.analytics import Anomaly, Forecast, KpiSnapshot, Target
from app.models.ai import (
    AIConversation,
    AIMessage,
    AIProviderConfig,
    AIRequest,
    AISuggestion,
    AITerminalCommand,
    AITerminalSession,
    AIUsageDaily,
)
from app.models.system import (
    AuditLog,
    BackupRecord,
    HealthCheckResult,
    Notification,
    NumberSequence,
    Setting,
    TrainingLesson,
    TrainingProgress,
)

__all__ = [
    "Base",
    # organization
    "Company", "Region", "Branch",
    # auth
    "User", "Role", "Permission", "RolePermission", "UserSession", "LoginAttempt",
    # product
    "Product", "ProductCategory", "Brand", "ProductUnit", "Barcode",
    "PriceList", "PriceListItem",
    # warehouse / stock
    "Warehouse", "Lot", "StockBalance", "StockMovement",
    "StockTransfer", "StockTransferItem", "StockCount", "StockCountItem",
    # vehicle / field
    "Vehicle", "Salesperson", "DaySession", "VanLoad", "VanLoadItem",
    # customer
    "Customer", "CustomerContact", "CustomerNote", "CustomerLedger",
    # route
    "Route", "RouteStop", "Visit", "GpsEvent",
    # sales
    "Order", "OrderItem", "Sale", "SaleItem", "Invoice", "InvoiceItem",
    "Payment", "PaymentAllocation", "ReturnDocument", "ReturnItem",
    # campaign
    "Campaign", "CampaignCondition", "CampaignApplication", "Discount",
    # analytics
    "Target", "Forecast", "Anomaly", "KpiSnapshot",
    # ai
    "AIProviderConfig", "AIRequest", "AIUsageDaily", "AIConversation", "AIMessage",
    "AISuggestion", "AITerminalSession", "AITerminalCommand",
    # system
    "Setting", "AuditLog", "Notification", "BackupRecord",
    "TrainingLesson", "TrainingProgress", "NumberSequence", "HealthCheckResult",
]
