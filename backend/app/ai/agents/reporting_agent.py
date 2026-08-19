"""
Reporting agent — turns a report dataset into an executive summary.

Takes whatever structured rows the reporting module produced and writes the
half-page a manager actually reads: what happened, what changed, what to do.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.ai import data_context
from app.ai.agents.base_agent import AgentResult, BaseAgent
from app.core.enums import AIAgentKind, AITaskType

#: Long tables blow the context window and add nothing: the summary is driven by
#: the head of the dataset plus the totals the caller supplies.
MAX_ROWS_IN_PROMPT = 60

SYSTEM_PROMPT_TR = """
Sen bir satış analisti olarak yöneticiye rapor özeti yazıyorsun.

Yapman gerekenler:
1. "YÖNETİCİ ÖZETİ" başlığıyla en fazla 3 cümlelik bir özet yaz.
2. "ÖNE ÇIKANLAR" başlığı altında rakamlarıyla birlikte en fazla 4 madde ver.
3. "DİKKAT" başlığı altında riskli veya olağandışı gördüğün en fazla 2 maddeyi yaz.
4. "AKSİYON" başlığı altında sorumlusu belli, ölçülebilir en fazla 3 öneri sırala.
Sayıları DATA'dan aynen al; oranları hesaplarken hangi iki sayıyı kullandığını belirt.
""".strip()

SYSTEM_PROMPT_EN = """
You are a sales analyst writing an executive summary of a report for management.

You must:
1. Write an "EXECUTIVE SUMMARY" heading with at most 3 sentences.
2. Under "HIGHLIGHTS", give at most 4 bullets, each with its figure.
3. Under "WATCH", list at most 2 risky or unusual observations.
4. Under "ACTIONS", list at most 3 measurable recommendations with an owner.
Take numbers verbatim from DATA; when you compute a ratio, name the two figures used.
""".strip()


class ReportingAgent(BaseAgent):
    """Summarises an arbitrary report dataset for management."""

    kind = str(AIAgentKind.REPORTING)
    task_type = str(AITaskType.REPORTING)
    max_tokens = 1800
    SYSTEM_PROMPT_TR = SYSTEM_PROMPT_TR
    SYSTEM_PROMPT_EN = SYSTEM_PROMPT_EN

    def run(
        self,
        db: Session,
        *,
        dataset: dict[str, Any] | list[dict[str, Any]] | None = None,
        report_name: str = "",
        language: str = "tr",
        user_id: int | None = None,
        conversation_id: int | None = None,
        question: str | None = None,
        period_days: int = 30,
        salesperson_ids: list[int] | None = None,
        **_: Any,
    ) -> AgentResult:
        if dataset is None:
            # No dataset supplied: summarise the company's recent trading instead
            # of refusing — that is the report a manager asks for by default.
            dataset = data_context.company_pulse(
                db, days=period_days, salesperson_ids=salesperson_ids
            )
            report_name = report_name or ("Genel Satış Özeti" if language != "en" else "Sales Overview")

        facts = {"report_name": report_name, "dataset": _trim(dataset)}
        default_question = (
            f"'{report_name}' raporu için yönetici özeti yaz."
            if language != "en"
            else f"Write an executive summary for the '{report_name}' report."
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
            suggestion=None,
            confidence=0.7 if facts["dataset"] else 0.2,
        )


def _trim(dataset: Any) -> Any:
    """Cap row lists so a wide report cannot overflow the context window."""
    if isinstance(dataset, list):
        return dataset[:MAX_ROWS_IN_PROMPT]
    if isinstance(dataset, dict):
        trimmed: dict[str, Any] = {}
        for key, value in dataset.items():
            if isinstance(value, list) and len(value) > MAX_ROWS_IN_PROMPT:
                trimmed[key] = value[:MAX_ROWS_IN_PROMPT]
                trimmed[f"{key}_truncated_from"] = len(value)
            else:
                trimmed[key] = value
        return trimmed
    return dataset


__all__ = ["ReportingAgent", "SYSTEM_PROMPT_EN", "SYSTEM_PROMPT_TR"]
