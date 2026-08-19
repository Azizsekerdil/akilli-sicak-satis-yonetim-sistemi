"""
Collection-risk agent — per-customer payment risk in plain commercial language.

Ageing buckets, bounced cheques and credit utilisation are all facts the system
already holds; what the collection team needs is a judgement about *this*
customer and a next step that fits the size of the exposure.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.ai import data_context
from app.ai.agents.base_agent import AgentResult, BaseAgent
from app.core.enums import AIAgentKind, AITaskType
from app.core.utils import clamp

SYSTEM_PROMPT_TR = """
Sen bir alacak yönetimi (tahsilat) uzmanısın. Bir müşterinin ödeme riskini
değerlendiriyorsun.

Yapman gerekenler:
1. Riski tek cümlede sınıflandır: DÜŞÜK / ORTA / YÜKSEK / KRİTİK ve nedenini söyle.
2. Yaşlandırma tablosundaki en ağır kalemi ve kaç gün geciktiğini belirt.
3. Kredi limiti kullanım oranını ve karşılıksız çek varsa etkisini değerlendir.
4. Bir sonraki ziyarette uygulanacak somut tahsilat aksiyonunu öner
   (nakit tahsilat, limit dondurma, vade yapılandırma gibi).
5. Müşteriyi kaybetme riski varsa ticari sonucu da belirt.
""".strip()

SYSTEM_PROMPT_EN = """
You are a receivables and collections specialist assessing one customer's
payment risk.

You must:
1. Classify the risk in one sentence: LOW / MEDIUM / HIGH / CRITICAL, with the reason.
2. Name the worst ageing bucket and how many days overdue it is.
3. Assess credit-limit utilisation and the impact of any bounced payments.
4. Recommend a concrete collection action for the next visit
   (cash collection, credit freeze, restructuring the term, …).
5. If there is a risk of losing the customer, state the commercial consequence too.
""".strip()


class CollectionRiskAgent(BaseAgent):
    """Scores and explains one customer's collection risk."""

    kind = str(AIAgentKind.COLLECTION_RISK)
    task_type = str(AITaskType.ANALYSIS)
    SYSTEM_PROMPT_TR = SYSTEM_PROMPT_TR
    SYSTEM_PROMPT_EN = SYSTEM_PROMPT_EN

    @staticmethod
    def _risk_band(snapshot: dict[str, Any]) -> dict[str, Any]:
        """
        Deterministic risk score computed here, not by the model.

        Weighted from overdue age, credit utilisation and bounced payments, so
        the same customer always scores the same regardless of which provider
        answered.
        """
        overdue_days = int(snapshot.get("oldest_overdue_days") or 0)
        utilisation = float(snapshot.get("credit_utilisation_percent") or 0.0)
        bounced = int(snapshot.get("bounced_payment_count") or 0)

        score = (
            clamp(overdue_days / 90 * 55, 0, 55)
            + clamp(utilisation / 100 * 30, 0, 30)
            + clamp(bounced * 7.5, 0, 15)
        )
        score = round(score, 1)
        if score >= 75:
            band = "CRITICAL"
        elif score >= 50:
            band = "HIGH"
        elif score >= 25:
            band = "MEDIUM"
        else:
            band = "LOW"
        return {
            "risk_score": score,
            "risk_band": band,
            "components": {
                "overdue_days": overdue_days,
                "credit_utilisation_percent": utilisation,
                "bounced_payments": bounced,
            },
        }

    def run(
        self,
        db: Session,
        *,
        customer_id: int,
        language: str = "tr",
        user_id: int | None = None,
        conversation_id: int | None = None,
        question: str | None = None,
        **_: Any,
    ) -> AgentResult:
        snapshot = data_context.collection_snapshot(db, customer_id)
        assessment = self._risk_band(snapshot)
        facts = {"customer": snapshot, "computed_risk": assessment}

        default_question = (
            f"{snapshot['name']} müşterisinin tahsilat riskini değerlendir."
            if language != "en"
            else f"Assess the collection risk for customer {snapshot['name']}."
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
            suggestion=assessment,
            confidence=0.8 if snapshot.get("open_invoice_count") else 0.4,
        )


__all__ = ["CollectionRiskAgent", "SYSTEM_PROMPT_EN", "SYSTEM_PROMPT_TR"]
