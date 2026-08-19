"""
Route agent — commentary on how well a route actually ran.

Compares planned against actual distance, duration and stop completion, then
explains the gap in the language a field supervisor uses: skipped customers,
kilometres per stop, strike rate, sales per stop.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.ai import data_context
from app.ai.agents.base_agent import AgentResult, BaseAgent
from app.core.enums import AIAgentKind, AITaskType

SYSTEM_PROMPT_TR = """
Sen bir saha satış operasyon şefisin. Bir rotanın günlük performansını değerlendiriyorsun.

Yapman gerekenler:
1. Rotanın genel verimliliğini tek cümlede özetle (tamamlanan durak / planlanan durak).
2. Planlanan ve gerçekleşen mesafe ile süre arasındaki farkı yorumla.
3. Atlanan müşteriler ve gecikmeler varsa nedenlerini DATA'daki bilgiye göre ele al.
4. Durak başına satış ve verimli ziyaret oranını değerlendir.
5. Yarınki rota için en fazla üç somut iyileştirme öner.
""".strip()

SYSTEM_PROMPT_EN = """
You are a field sales operations supervisor reviewing how a route performed today.

You must:
1. Summarise overall efficiency in one sentence (completed stops vs planned stops).
2. Interpret the gap between planned and actual distance and duration.
3. Address skipped customers and delays using the reasons present in DATA.
4. Assess sales per stop and the productive-visit (strike) rate.
5. Recommend at most three concrete improvements for tomorrow's route.
""".strip()


class RouteAgent(BaseAgent):
    """Narrates route efficiency from planned-versus-actual figures."""

    kind = str(AIAgentKind.ROUTE)
    task_type = str(AITaskType.ANALYSIS)
    SYSTEM_PROMPT_TR = SYSTEM_PROMPT_TR
    SYSTEM_PROMPT_EN = SYSTEM_PROMPT_EN

    def run(
        self,
        db: Session,
        *,
        route_id: int,
        language: str = "tr",
        user_id: int | None = None,
        conversation_id: int | None = None,
        question: str | None = None,
        **_: Any,
    ) -> AgentResult:
        snapshot = data_context.route_efficiency(db, route_id)
        facts = {"route": snapshot}
        label = snapshot.get("name") or snapshot.get("code") or f"#{route_id}"
        default_question = (
            f"{label} rotasının verimliliğini değerlendir."
            if language != "en"
            else f"Assess the efficiency of route {label}."
        )
        result, error_key = self.narrate(
            db,
            question=question or default_question,
            facts=facts,
            language=language,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        # Completion ratio doubles as the confidence in the commentary: a route
        # with almost no completed stops has little to comment on.
        completion = 0.0
        planned = snapshot.get("planned_stops") or 0
        if planned:
            completion = min(1.0, (snapshot.get("completed_stops") or 0) / planned)
        return self.build_result(
            data_context=facts,
            result=result,
            error_key=error_key,
            suggestion=snapshot,
            confidence=round(completion, 2),
        )


__all__ = ["RouteAgent", "SYSTEM_PROMPT_EN", "SYSTEM_PROMPT_TR"]
