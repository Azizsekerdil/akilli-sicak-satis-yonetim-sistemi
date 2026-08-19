"""
Inventory agent — explains a suggested van load.

Loading a van is a constrained decision: expected demand against vehicle volume
and weight, against what the warehouse actually has.  The agent makes those
trade-offs visible so the warehouse and the salesperson agree before the doors
close.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.ai import data_context
from app.ai.agents.base_agent import AgentResult, BaseAgent
from app.core.enums import AIAgentKind, AITaskType

SYSTEM_PROMPT_TR = """
Sen bir depo ve araç yükleme planlama uzmanısın. Bir plasiyerin aracına önerilen
yüklemeyi açıklıyorsun.

Yapman gerekenler:
1. Önerilen yüklemeyi tek cümlede özetle (kaç kalem, toplam hacim ve ağırlık).
2. Araç kapasitesinin yüzde kaçının kullanıldığını söyle; %90 üzerindeyse uyar.
3. En yüksek miktarlı 3-5 ürünü günlük ortalama satışa dayanarak gerekçelendir.
4. Depoda yeterli stok olmadığı için kısılan kalemler varsa bunları ayrıca belirt.
5. Yükleme öncesi kontrol edilmesi gereken tek bir riski söyle.
""".strip()

SYSTEM_PROMPT_EN = """
You are a warehouse and van-loading planner explaining a suggested vehicle load
to the loading team and the salesperson.

You must:
1. Summarise the proposed load in one sentence (line count, total volume and weight).
2. State what percentage of vehicle capacity is used; warn above 90%.
3. Justify the 3-5 largest quantities using average daily sales.
4. Call out any line that was cut back because warehouse stock was short.
5. Name the single biggest risk to check before loading.
""".strip()


class InventoryAgent(BaseAgent):
    """Narrates a van-load proposal against capacity and stock reality."""

    kind = str(AIAgentKind.INVENTORY)
    task_type = str(AITaskType.ANALYSIS)
    SYSTEM_PROMPT_TR = SYSTEM_PROMPT_TR
    SYSTEM_PROMPT_EN = SYSTEM_PROMPT_EN

    def run(
        self,
        db: Session,
        *,
        salesperson_id: int,
        vehicle_id: int | None = None,
        on_date: date | None = None,
        language: str = "tr",
        user_id: int | None = None,
        conversation_id: int | None = None,
        question: str | None = None,
        **_: Any,
    ) -> AgentResult:
        suggestion = data_context.van_load_suggestion(
            db, salesperson_id=salesperson_id, vehicle_id=vehicle_id, on_date=on_date
        )
        facts = {"van_load": suggestion}
        who = suggestion.get("salesperson_name") or f"#{salesperson_id}"
        default_question = (
            f"{who} için önerilen araç yüklemesini açıkla."
            if language != "en"
            else f"Explain the suggested van load for {who}."
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


__all__ = ["InventoryAgent", "SYSTEM_PROMPT_EN", "SYSTEM_PROMPT_TR"]
