"""
Orchestrator — decides which specialist agent should answer.

Routing is keyword-first and model-second on purpose.  A deterministic classifier
is instant, free, works when every provider is down, and never routes "müşterinin
borcu ne kadar?" to the forecast agent because a model felt creative.  The model
is only consulted when the keywords are genuinely ambiguous, and even then its
answer is constrained to the known agent vocabulary.

Whatever the route, the chosen agent is the one that gathers the data — the
orchestrator itself never invents figures.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import data_context
from app.ai.agents.base_agent import AgentResult, BaseAgent
from app.ai.agents.collection_risk_agent import CollectionRiskAgent
from app.ai.agents.data_analyst_agent import DataAnalystAgent
from app.ai.agents.forecast_agent import ForecastAgent
from app.ai.agents.inventory_agent import InventoryAgent
from app.ai.agents.reporting_agent import ReportingAgent
from app.ai.agents.route_agent import RouteAgent
from app.ai.agents.sales_agent import SalesAgent
from app.core.enums import AIAgentKind, AITaskType
from app.core.logging_config import get_logger
from app.core.utils import tr_lower
from app.models.customer import Customer
from app.models.product import Product
from app.models.route import Route
from app.models.vehicle import Salesperson

log = get_logger("app.ai.orchestrator")

SYSTEM_PROMPT_TR = """
Sen bir yönlendirme uzmanısın. Kullanıcının sorusunu okuyup hangi uzman ajanın
cevaplaması gerektiğine karar veriyorsun.

Seçenekler:
- SALES: müşteriye özel sipariş önerisi, ne satılmalı
- FORECAST: ürün talep tahmini, gelecek dönem beklentisi
- ROUTE: rota verimliliği, ziyaret planı, durak analizi
- INVENTORY: araç yükleme, depo stoğu, yüklenecek miktar
- COLLECTION_RISK: müşteri borcu, vade, tahsilat riski
- DATA_ANALYST: veritabanından sayısal sorgu gerektiren sorular
- REPORTING: genel performans özeti, yönetici raporu

Cevabın SADECE bu listeden tek bir kod olsun. Başka hiçbir şey yazma.
""".strip()

SYSTEM_PROMPT_EN = """
You are a routing specialist. You read the user's question and decide which
specialist agent should answer it.

Options:
- SALES: customer-specific order suggestion, what to sell
- FORECAST: product demand forecast, expected future volume
- ROUTE: route efficiency, visit plan, stop analysis
- INVENTORY: van loading, warehouse stock, quantities to load
- COLLECTION_RISK: customer debt, payment terms, collection risk
- DATA_ANALYST: questions needing a numeric query against the database
- REPORTING: general performance summary, management report

Reply with EXACTLY ONE code from that list and nothing else.
""".strip()

#: Lower-cased trigger words per agent.  Turkish and English live together so a
#: mixed-language question ("route verimliliği") still routes correctly.
INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    str(AIAgentKind.SALES): (
        "siparis", "siparis oner", "ne satayim", "satis oner", "urun oner",
        "upsell", "capraz satis", "order", "suggest order", "what to sell",
        "recommend product", "basket",
    ),
    str(AIAgentKind.FORECAST): (
        "tahmin", "talep", "gelecek hafta", "gelecek ay", "onumuzdeki",
        "forecast", "demand", "projection", "next week", "next month",
    ),
    str(AIAgentKind.ROUTE): (
        "rota", "guzergah", "durak", "ziyaret plani", "km", "route", "stop",
        "itinerary", "visit plan", "mileage",
    ),
    str(AIAgentKind.INVENTORY): (
        "yukleme", "arac yuku", "yuklenecek", "depo", "stok", "van load",
        "loading", "load plan", "inventory", "warehouse",
    ),
    str(AIAgentKind.COLLECTION_RISK): (
        "tahsilat", "borc", "alacak", "vade", "gecikme", "risk", "kredi limiti",
        "cek", "collection", "debt", "overdue", "receivable", "credit limit",
        "payment risk",
    ),
    str(AIAgentKind.DATA_ANALYST): (
        "sql", "sorgu", "kac ", "ne kadar", "listele", "siralama", "en cok",
        "en az", "toplam", "ortalama", "query", "how many", "how much",
        "list ", "top ", "total", "average", "count",
    ),
    str(AIAgentKind.REPORTING): (
        "rapor", "ozet", "performans", "genel durum", "nasil gidiyor",
        "report", "summary", "performance", "overview", "how are we doing",
    ),
}

AGENTS: dict[str, type[BaseAgent]] = {
    str(AIAgentKind.SALES): SalesAgent,
    str(AIAgentKind.FORECAST): ForecastAgent,
    str(AIAgentKind.ROUTE): RouteAgent,
    str(AIAgentKind.INVENTORY): InventoryAgent,
    str(AIAgentKind.COLLECTION_RISK): CollectionRiskAgent,
    str(AIAgentKind.REPORTING): ReportingAgent,
    str(AIAgentKind.DATA_ANALYST): DataAnalystAgent,
}

#: Agents that cannot work without a subject id.
REQUIRED_SUBJECT: dict[str, str] = {
    str(AIAgentKind.SALES): "customer_id",
    str(AIAgentKind.FORECAST): "product_id",
    str(AIAgentKind.ROUTE): "route_id",
    str(AIAgentKind.INVENTORY): "salesperson_id",
    str(AIAgentKind.COLLECTION_RISK): "customer_id",
}

_ID_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?:musteri|customer)\s*#?\s*(\d+)", "customer_id"),
    (r"(?:urun|product|sku)\s*#?\s*(\d+)", "product_id"),
    (r"(?:rota|route)\s*#?\s*(\d+)", "route_id"),
    (r"(?:plasiyer|salesperson)\s*#?\s*(\d+)", "salesperson_id"),
)


def _normalise(text: str) -> str:
    """Fold Turkish casing and diacritics so keyword matching is stable."""
    folded = tr_lower(text or "")
    table = str.maketrans("çğıöşü", "cgiosu")
    return folded.translate(table)


def score_intents(question: str) -> dict[str, int]:
    """How many trigger words each agent matches in *question*."""
    normalised = _normalise(question)
    return {
        kind: sum(1 for word in words if word in normalised)
        for kind, words in INTENT_KEYWORDS.items()
    }


def extract_ids(question: str) -> dict[str, int]:
    """Pick up explicit ``customer 42`` / ``rota 7`` style references."""
    normalised = _normalise(question)
    found: dict[str, int] = {}
    for pattern, key in _ID_PATTERNS:
        match = re.search(pattern, normalised)
        if match:
            found[key] = int(match.group(1))
    return found


def resolve_entities(db: Session, question: str) -> dict[str, Any]:
    """
    Match names mentioned in the question against real records.

    Uses ``lower(col) LIKE '%term%'`` rather than ILIKE so the same query works
    on SQLite and PostgreSQL.
    """
    found = extract_ids(question)
    words = [w for w in re.split(r"[^\wğüşıöçĞÜŞİÖÇ]+", question or "") if len(w) >= 4]
    if not words:
        return found

    for word in words[:6]:
        term = tr_lower(word)
        if "customer_id" not in found:
            row = db.execute(
                select(Customer.id)
                .where(
                    func.lower(Customer.name).like(f"%{term}%"),
                    Customer.is_deleted.is_(False),
                )
                .limit(2)
            ).scalars().all()
            if len(row) == 1:
                found["customer_id"] = int(row[0])
        if "product_id" not in found:
            row = db.execute(
                select(Product.id)
                .where(
                    func.lower(Product.name).like(f"%{term}%"),
                    Product.is_deleted.is_(False),
                )
                .limit(2)
            ).scalars().all()
            if len(row) == 1:
                found["product_id"] = int(row[0])
        if "route_id" not in found:
            row = db.execute(
                select(Route.id)
                .where(
                    func.lower(Route.name).like(f"%{term}%"),
                    Route.is_deleted.is_(False),
                )
                .limit(2)
            ).scalars().all()
            if len(row) == 1:
                found["route_id"] = int(row[0])
        if "salesperson_id" not in found:
            row = db.execute(
                select(Salesperson.id)
                .where(
                    func.lower(Salesperson.full_name).like(f"%{term}%"),
                    Salesperson.is_deleted.is_(False),
                )
                .limit(2)
            ).scalars().all()
            if len(row) == 1:
                found["salesperson_id"] = int(row[0])
    return found


class OrchestratorAgent(BaseAgent):
    """Routes a free-text request to the specialist that owns the answer."""

    kind = str(AIAgentKind.ORCHESTRATOR)
    task_type = str(AITaskType.GENERAL)
    SYSTEM_PROMPT_TR = SYSTEM_PROMPT_TR
    SYSTEM_PROMPT_EN = SYSTEM_PROMPT_EN

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #
    def classify(
        self,
        db: Session,
        question: str,
        *,
        language: str = "tr",
        use_model: bool = True,
        user_id: int | None = None,
    ) -> tuple[str, str]:
        """
        Return ``(agent_kind, how)`` where *how* is ``keywords`` or ``model``.

        The model is consulted only when no keyword fires, and its answer is
        accepted only if it names a known agent.
        """
        scores = score_intents(question)
        best = max(scores, key=lambda k: scores[k])
        if scores[best] > 0:
            return best, "keywords"

        if use_model:
            result, _ = self.narrate(
                db,
                question=question,
                facts={"available_agents": sorted(AGENTS)},
                language=language,
                user_id=user_id,
                max_tokens=600,
            )
            if result:
                answer = (result.content or "").strip().upper()
                for kind in AGENTS:
                    if kind in answer:
                        return kind, "model"
        return str(AIAgentKind.REPORTING), "default"

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def run(
        self,
        db: Session,
        *,
        question: str,
        language: str = "tr",
        user_id: int | None = None,
        conversation_id: int | None = None,
        agent_kind: str | None = None,
        salesperson_ids: list[int] | None = None,
        **params: Any,
    ) -> AgentResult:
        chosen = str(agent_kind).upper() if agent_kind else None
        how = "explicit"
        if chosen not in AGENTS:
            chosen, how = self.classify(
                db, question, language=language, user_id=user_id
            )

        entities = resolve_entities(db, question)
        entities.update({k: v for k, v in params.items() if v is not None})

        needed = REQUIRED_SUBJECT.get(chosen)
        if needed and not entities.get(needed):
            # The specialist has no subject to work on: answer the question as a
            # general performance summary rather than erroring on the user.
            log.info("Orchestrator fell back to REPORTING: %s missing for %s", needed, chosen)
            chosen, how = str(AIAgentKind.REPORTING), f"{how}->fallback_missing_{needed}"

        agent = AGENTS[chosen]()
        kwargs: dict[str, Any] = {
            "language": language,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "question": question,
        }
        if chosen in (str(AIAgentKind.SALES), str(AIAgentKind.COLLECTION_RISK)):
            kwargs["customer_id"] = entities["customer_id"]
        elif chosen == str(AIAgentKind.FORECAST):
            kwargs["product_id"] = entities["product_id"]
            if entities.get("horizon_days"):
                kwargs["horizon_days"] = entities["horizon_days"]
        elif chosen == str(AIAgentKind.ROUTE):
            kwargs["route_id"] = entities["route_id"]
        elif chosen == str(AIAgentKind.INVENTORY):
            kwargs["salesperson_id"] = entities["salesperson_id"]
            kwargs["vehicle_id"] = entities.get("vehicle_id")
        elif chosen == str(AIAgentKind.REPORTING):
            kwargs["salesperson_ids"] = salesperson_ids
            kwargs["dataset"] = params.get("dataset")
            kwargs["report_name"] = params.get("report_name", "")

        result = agent.run(db, **kwargs)
        result.data_context.setdefault("routing", {})
        result.data_context["routing"] = {
            "selected_agent": chosen,
            "decided_by": how,
            "entities": entities,
        }
        return result

    # ------------------------------------------------------------------ #
    # General knowledge fallback
    # ------------------------------------------------------------------ #
    def answer_general(
        self,
        db: Session,
        *,
        question: str,
        language: str = "tr",
        user_id: int | None = None,
        conversation_id: int | None = None,
        salesperson_ids: list[int] | None = None,
    ) -> AgentResult:
        """Answer with the company's recent trading figures as the sole context."""
        facts = {"company_pulse": data_context.company_pulse(db, salesperson_ids=salesperson_ids)}
        result, error_key = self.narrate(
            db,
            question=question,
            facts=facts,
            language=language,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return self.build_result(
            data_context=facts, result=result, error_key=error_key, confidence=0.5
        )


__all__ = [
    "AGENTS",
    "OrchestratorAgent",
    "SYSTEM_PROMPT_EN",
    "SYSTEM_PROMPT_TR",
    "extract_ids",
    "resolve_entities",
    "score_intents",
]
