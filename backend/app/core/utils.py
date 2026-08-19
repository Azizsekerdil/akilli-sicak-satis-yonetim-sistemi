"""Small shared helpers: JSON, money maths, geo distance, pagination, slugs."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, TypeVar

T = TypeVar("T")

# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
class _Encoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, set):
            return sorted(o)
        if hasattr(o, "__dict__"):
            return {k: v for k, v in o.__dict__.items() if not k.startswith("_")}
        return str(o)


def dumps(obj: Any, *, indent: int | None = None) -> str:
    """JSON-serialise, handling Decimal/date/datetime.  Always UTF-8 safe."""
    return json.dumps(obj, cls=_Encoder, ensure_ascii=False, indent=indent)


def loads(raw: str | None, default: T = None) -> Any | T:  # type: ignore[assignment]
    """Parse JSON, returning *default* instead of raising on bad input."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Money & quantity maths
# ---------------------------------------------------------------------------
MONEY_EXP = Decimal("0.0001")
QTY_EXP = Decimal("0.001")
DISPLAY_EXP = Decimal("0.01")


def D(value: Any) -> Decimal:
    """Coerce anything sane into a Decimal, never raising on None/''/junk."""
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def money(value: Any) -> Decimal:
    """Quantise to 4 decimal places (storage precision)."""
    return D(value).quantize(MONEY_EXP, rounding=ROUND_HALF_UP)


def qty(value: Any) -> Decimal:
    """Quantise to 3 decimal places."""
    return D(value).quantize(QTY_EXP, rounding=ROUND_HALF_UP)


def display_money(value: Any) -> Decimal:
    """Quantise to 2 decimal places (invoice / UI presentation)."""
    return D(value).quantize(DISPLAY_EXP, rounding=ROUND_HALF_UP)


def pct(part: Any, whole: Any, *, ndigits: int = 2) -> float:
    """Safe percentage — returns 0.0 rather than dividing by zero."""
    w = D(whole)
    if w == 0:
        return 0.0
    return round(float(D(part) / w * 100), ndigits)


def apply_percent(amount: Any, percent: float) -> Decimal:
    """Return the *discount amount* for ``percent`` off ``amount``."""
    return money(D(amount) * D(percent) / Decimal("100"))


def vat_from_net(net: Any, vat_rate: float) -> Decimal:
    return money(D(net) * D(vat_rate) / Decimal("100"))


def net_from_gross(gross: Any, vat_rate: float) -> Decimal:
    """Strip VAT out of a VAT-inclusive amount."""
    return money(D(gross) / (Decimal("1") + D(vat_rate) / Decimal("100")))


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two WGS-84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def road_distance_km(lat1: float, lon1: float, lat2: float, lon2: float, factor: float = 1.35) -> float:
    """
    Straight-line distance inflated by a detour factor.

    Real road networks are ~1.3-1.4x the crow-flight distance in Turkish urban
    areas; this keeps routing sane without requiring an external routing server.
    """
    return haversine_km(lat1, lon1, lat2, lon2) * factor


def travel_time_minutes(distance_km: float, avg_speed_kmh: float = 30.0) -> int:
    if avg_speed_kmh <= 0:
        return 0
    return int(round(distance_km / avg_speed_kmh * 60))


# ---------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------
_TR_MAP = str.maketrans("çÇğĞıİöÖşŞüÜ", "cCgGiIoOsSuU")


def slugify(text: str, *, sep: str = "-") -> str:
    """ASCII slug that handles Turkish characters correctly (İ -> I, ı -> i)."""
    s = (text or "").translate(_TR_MAP)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", sep, s).strip(sep).lower()
    return s or "item"


def tr_upper(text: str) -> str:
    """Turkish-correct uppercase: 'istanbul' -> 'İSTANBUL', not 'ISTANBUL'."""
    return (text or "").replace("i", "İ").replace("ı", "I").upper()


def tr_lower(text: str) -> str:
    """Turkish-correct lowercase: 'İSTANBUL' -> 'istanbul'."""
    return (text or "").replace("I", "ı").replace("İ", "i").lower()


def normalize_search(text: str) -> str:
    """Fold a string for accent/case-insensitive matching."""
    return slugify(text, sep=" ")


def truncate(text: str | None, limit: int, suffix: str = "…") -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - len(suffix)] + suffix


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def weekday_code(d: date) -> str:
    return WEEKDAY_CODES[d.weekday()]


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    if d.month == 12:
        return d.replace(day=31)
    return d.replace(month=d.month + 1, day=1) - timedelta(days=1)


def add_months(d: date, months: int) -> date:
    """Shift by whole months, clamping the day to the target month's length."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    last = month_end(date(year, month, 1)).day
    return date(year, month, min(d.day, last))


def date_range(start: date, end: date) -> list[date]:
    if end < start:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value)[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_hhmm(value: str | None) -> int | None:
    """'08:30' -> 510 minutes since midnight."""
    if not value:
        return None
    try:
        h, m = value.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def format_hhmm(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    minutes = max(0, int(minutes))
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
def chunked(items: list[T], size: int) -> list[list[T]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_div(a: Any, b: Any, default: float = 0.0) -> float:
    try:
        bb = float(b)
        return float(a) / bb if bb else default
    except (TypeError, ValueError, ZeroDivisionError):
        return default


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
def is_loopback_address(value: str | None) -> bool:
    """
    True when *value* is the machine the server itself runs on.

    Used by the first-run bootstrap gate: the initial administrator account may
    only sign in from the local device until its password has been changed.

    Anything that cannot be parsed as an IP address is treated as **remote**.
    That is the safe direction — an unparseable value is usually a proxy header
    the operator has not configured properly, and guessing "local" there would
    hand the bootstrap account to the whole network.

    A bare hostname is likewise remote: ``localhost`` is not accepted, because
    a hostname is attacker-influenceable in a way an address is not.  Starlette
    gives us ``request.client.host``, which is always an address.
    """
    if not value:
        return False
    # Strip an IPv6 zone index ("fe80::1%eth0") and brackets ("[::1]").
    raw = value.strip().strip("[]").split("%", 1)[0]
    if not raw:
        return False
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    # "::ffff:127.0.0.1" — an IPv4 loopback wearing an IPv6 coat.
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)
