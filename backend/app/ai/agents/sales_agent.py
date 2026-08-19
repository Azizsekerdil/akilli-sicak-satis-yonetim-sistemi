"""
Sales agent — per-customer order suggestions with a human explanation.

The numbers come from the analytics service's ``suggest_order`` (or, when that
is unavailable, from the customer's own purchase cycle); the model's only job is
to turn them into something a salesperson can read at the shop door in thirty
seconds and act on.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.ai import data_context
from app.ai.agents.base_agent import AgentResult, BaseAgent
from app.core.enums import AIAgentKind, AITaskType

SYSTEM_PROMPT_TR = """
Sen bir sıcak satış (van sales) operasyonunda çalışan kıdemli bir satış danışmanısın.
Görevin: bir müşteri için hazırlanmış sipariş önerisini plasiyere açıklamak.

Yapman gerekenler:
1. Önerinin özetini bir cümlede ver (kaç kalem, yaklaşık ne kadar hacim).
2. En kritik 3-5 ürünü, DATA'daki geçmiş alım sıklığı ve son alım tarihine dayanarak gerekçelendir.
3. Müşterinin cari bakiyesi ve kredi limiti riskliyse uyar.
4. Plasiyere ziyarette söyleyeceği tek cümlelik bir açılış cümlesi öner.
""".strip()

SYSTEM_PROMPT_EN = """
You are a senior sales advisor working in a van-sales (direct store delivery) operation.
Your task: explain a prepared order suggestion to the field salesperson.

You must:
1. Summarise the suggestion in one sentence (how many lines, roughly what volume).
2. Justify the 3-5 most important products using the purchase frequency and last
   purchase date in DATA.
3. Warn if the customer's balance or credit limit makes the order risky.
4. Propose a single opening sentence the salesperson can say at the door.
""".strip()


class SalesAgent(BaseAgent):
    """Turns a computed order proposal into field-ready advice."""

    kind = str(AIAgentKind.SALES)
    task_type = str(AITaskType.ANALYSIS)
    SYSTEM_PROMPT_TR = SYSTEM_PROMPT_TR
    SYSTEM_PROMPT_EN = SYSTEM_PROMPT_EN

    def run(
        self,
        db: Session,
        *,
        customer_id: int,
        language: str = "tr",
        on_date: date | None = None,
        user_id: int | None = None,
        conversation_id: int | None = None,
        question: str | None = None,
        **_: Any,
    ) -> AgentResult:
        customer = data_context.customer_snapshot(db, customer_id)
        suggestion = data_context.order_suggestion(db, customer_id, on_date=on_date)
        history = data_context.customer_product_history(db, customer_id, days=120, limit=12)

        facts = {
            "customer": customer,
            "order_suggestion": suggestion,
            "purchase_history": history,
        }
        default_question = (
            f"{customer['name']} müşterisi için sipariş önerisini açıkla."
            if language != "en"
            else f"Explain the order suggestion for customer {customer['name']}."
        )
        result, error_key = self.narrate(
            db,
            question=question or default_question,
            facts=facts,
            language=language,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return self.build_result(
            data_context=facts,
            result=result,
            error_key=error_key,
            suggestion=suggestion,
            confidence=float(suggestion.get("confidence") or 0.0),
        )


__all__ = ["SalesAgent", "SYSTEM_PROMPT_EN", "SYSTEM_PROMPT_TR"]
