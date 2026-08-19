"""
Forecast agent — a demand narrative on top of a numeric forecast.

A forecast number without a reason is not actionable: the planner needs to know
whether the trend is rising, how volatile the history is, and how much of the
answer to trust.  This agent supplies that, strictly from the computed series.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.ai import data_context
from app.ai.agents.base_agent import AgentResult, BaseAgent
from app.core.enums import AIAgentKind, AITaskType

SYSTEM_PROMPT_TR = """
Sen bir talep planlama uzmanısın. Yiyecek-içecek dağıtımında ürün bazlı talep
tahminlerini yorumluyorsun.

Yapman gerekenler:
1. Tahmini tek cümlede söyle: önümüzdeki dönemde beklenen miktar ve güven aralığı.
2. Haftalık geçmiş seriye bakarak trendi (artış/azalış/durağan) yüzde ile açıkla.
3. Serideki dalgalanma yüksekse tahminin neden temkinli okunması gerektiğini belirt.
4. Stok ve üretim planı için tek bir somut aksiyon öner.
""".strip()

SYSTEM_PROMPT_EN = """
You are a demand planning specialist interpreting product-level demand forecasts
for a food & beverage distribution business.

You must:
1. State the forecast in one sentence: expected quantity and confidence range.
2. Explain the trend (up/down/flat) as a percentage, using the weekly history.
3. If the series is volatile, say why the forecast should be read cautiously.
4. Recommend exactly one concrete stock or replenishment action.
""".strip()


class ForecastAgent(BaseAgent):
    """Explains a demand forecast in planning language."""

    kind = str(AIAgentKind.FORECAST)
    task_type = str(AITaskType.ANALYSIS)
    SYSTEM_PROMPT_TR = SYSTEM_PROMPT_TR
    SYSTEM_PROMPT_EN = SYSTEM_PROMPT_EN

    def run(
        self,
        db: Session,
        *,
        product_id: int,
        horizon_days: int = 14,
        customer_id: int | None = None,
        language: str = "tr",
        user_id: int | None = None,
        conversation_id: int | None = None,
        question: str | None = None,
        **_: Any,
    ) -> AgentResult:
        forecast = data_context.demand_forecast(
            db, product_id, horizon_days=horizon_days, customer_id=customer_id
        )
        facts = {"forecast": forecast}
        name = forecast.get("product_name") or f"#{product_id}"
        default_question = (
            f"{name} ürünü için {horizon_days} günlük talep tahminini yorumla."
            if language != "en"
            else f"Interpret the {horizon_days}-day demand forecast for product {name}."
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
            suggestion=forecast,
            confidence=float(forecast.get("confidence") or 0.0),
        )


__all__ = ["ForecastAgent", "SYSTEM_PROMPT_EN", "SYSTEM_PROMPT_TR"]
