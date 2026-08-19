"""
Data-analyst agent — natural language in, checked SQL out, numbers explained.

The model never touches the database.  It proposes SQL, :mod:`app.ai.sql_guard`
decides whether that SQL is a single read against non-sensitive tables, the
application executes it, and only then does the model see the rows it asked for.

A rejected query gets exactly one repair attempt, with the rejection reason fed
back in.  More than that and a model that keeps trying to write DML is simply
told no.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.ai import sql_guard
from app.ai.agents.base_agent import AgentResult, BaseAgent
from app.ai.base import ChatMessage
from app.ai.router import ai_router
from app.core.enums import AIAgentKind, AITaskType
from app.core.exceptions import AIBudgetExceededError, AIProviderError, UnsafeQueryError
from app.core.logging_config import get_logger

log = get_logger("app.ai.analyst")

MAX_REPAIR_ATTEMPTS = 1

SYSTEM_PROMPT_TR = """
Sen bir veri analistisin ve sadece OKUMA amaçlı SQL yazıyorsun.

Kurallar:
- Yalnızca tek bir SELECT (veya WITH ... SELECT) sorgusu üret.
- INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA, ATTACH kullanma.
- Yorum satırı (-- veya /* */) ve noktalı virgülle ayrılmış ikinci bir ifade yazma.
- Sadece sana verilen şemadaki tablo ve kolonları kullan.
- Sorgu SQLite ve PostgreSQL'de aynı çalışmalı: ILIKE, JSONB, ARRAY kullanma;
  büyük/küçük harf duyarsız arama için lower(kolon) LIKE '%...%' yaz.
- En fazla 1000 satır dönecek şekilde LIMIT ekle.
- ÇIKTIN SADECE SQL OLSUN. Açıklama, markdown veya kod bloğu işareti ekleme.
""".strip()

SYSTEM_PROMPT_EN = """
You are a data analyst who writes read-only SQL.

Rules:
- Produce exactly one SELECT (or WITH ... SELECT) statement.
- Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA or ATTACH.
- No comments (-- or /* */) and no second statement after a semicolon.
- Use only tables and columns from the schema you are given.
- The query must run identically on SQLite and PostgreSQL: no ILIKE, JSONB or ARRAY;
  for case-insensitive matching write lower(column) LIKE '%...%'.
- Add a LIMIT so at most 1000 rows are returned.
- OUTPUT SQL ONLY. No prose, no markdown, no code fences.
""".strip()

EXPLAIN_PROMPT_TR = """
Sen bir satış veri analistisin. Çalıştırılmış bir sorgunun sonuçlarını yorumluyorsun.

Yapman gerekenler:
1. Soruyu tek cümlede cevapla, cevabın içinde asıl rakamı ver.
2. En dikkat çekici 2-3 satırı rakamlarıyla açıkla.
3. Sonuç kesilmişse (truncated) bunu belirt.
4. Veri sorunun tamamını cevaplamıyorsa neyin eksik olduğunu söyle.
""".strip()

EXPLAIN_PROMPT_EN = """
You are a sales data analyst interpreting the results of an executed query.

You must:
1. Answer the question in one sentence, containing the key figure.
2. Explain the 2-3 most notable rows with their numbers.
3. Say so if the result set was truncated.
4. If the data does not fully answer the question, state what is missing.
""".strip()

_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_sql(text: str) -> str:
    """Pull the statement out of a model answer that may be fenced or chatty."""
    if not text:
        return ""
    fenced = _FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    candidate = candidate.strip()
    # Drop any leading prose before the first SELECT/WITH.
    match = re.search(r"\b(SELECT|WITH)\b", candidate, re.IGNORECASE)
    return candidate[match.start():].strip() if match else candidate


class DataAnalystAgent(BaseAgent):
    """Natural-language questions answered with guarded, read-only SQL."""

    kind = str(AIAgentKind.DATA_ANALYST)
    task_type = str(AITaskType.SQL)
    temperature = 0.0
    max_tokens = 1200
    SYSTEM_PROMPT_TR = SYSTEM_PROMPT_TR
    SYSTEM_PROMPT_EN = SYSTEM_PROMPT_EN

    # ------------------------------------------------------------------ #
    # SQL generation
    # ------------------------------------------------------------------ #
    def generate_sql(
        self,
        db: Session,
        *,
        question: str,
        language: str,
        user_id: int | None = None,
        conversation_id: int | None = None,
    ) -> tuple[str, str | None, dict[str, Any]]:
        """
        Ask the model for SQL and validate it, repairing once on rejection.

        Returns ``(sql, error_key, meta)``; ``sql`` is empty when no provider
        answered or every attempt was rejected.
        """
        schema = sql_guard.schema_summary()
        prompt_label = "SORU" if language != "en" else "QUESTION"
        base_prompt = (
            f"{prompt_label}:\n{question}\n\n"
            f"{'ŞEMA' if language != 'en' else 'SCHEMA'}:\n{schema}"
        )
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt(language)),
            ChatMessage(role="user", content=base_prompt),
        ]

        meta: dict[str, Any] = {"attempts": []}
        for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
            try:
                result = ai_router.chat(
                    db,
                    messages,
                    task_type=self.task_type,
                    agent_kind=self.kind,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                )
            except AIBudgetExceededError:
                return "", "ai.budget_exceeded", meta
            except AIProviderError as exc:
                return "", exc.message_key, meta

            candidate = extract_sql(result.content)
            meta["provider"] = result.provider
            meta["model"] = result.model
            try:
                sql_guard.validate(candidate)
                meta["attempts"].append({"sql": candidate, "accepted": True})
                return candidate, None, meta
            except UnsafeQueryError as exc:
                reason = str(exc.params.get("reason") or exc.message_key)
                meta["attempts"].append(
                    {"sql": candidate, "accepted": False, "reason": reason}
                )
                if attempt >= MAX_REPAIR_ATTEMPTS:
                    meta["rejection_reason"] = reason
                    return "", "ai.sql_forbidden", meta
                messages.append(ChatMessage(role="assistant", content=candidate))
                messages.append(
                    ChatMessage(
                        role="user",
                        content=(
                            f"Bu sorgu güvenlik kontrolünden geçmedi: {reason}. "
                            "Sadece tek bir SELECT üret ve tekrar dene."
                            if language != "en"
                            else f"That query failed the safety check: {reason}. "
                            "Produce a single SELECT and try again."
                        ),
                    )
                )
        return "", "ai.sql_forbidden", meta

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def run(
        self,
        db: Session,
        *,
        question: str,
        sql: str | None = None,
        language: str = "tr",
        max_rows: int = sql_guard.DEFAULT_MAX_ROWS,
        explain: bool = True,
        user_id: int | None = None,
        conversation_id: int | None = None,
        **_: Any,
    ) -> AgentResult:
        meta: dict[str, Any] = {}
        error_key: str | None = None

        if sql:
            # An analyst supplied the SQL directly — it still goes through the
            # same guard; a hand-written query is not automatically trusted.
            statement = sql
        else:
            statement, error_key, meta = self.generate_sql(
                db,
                question=question,
                language=language,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            if not statement:
                return self.build_result(
                    data_context={"question": question, "sql": None, "meta": meta},
                    result=None,
                    error_key=error_key,
                    confidence=0.0,
                )

        query_result = sql_guard.run_readonly(db, statement, max_rows=max_rows)
        facts = {
            "question": question,
            "sql": query_result["sql"],
            "columns": query_result["columns"],
            "row_count": query_result["row_count"],
            "truncated": query_result["truncated"],
            "rows": query_result["rows"][:100],
            "generation": meta,
        }
        if not explain:
            return self.build_result(
                data_context=facts, result=None, error_key=None, confidence=0.6
            )

        explain_prompt = EXPLAIN_PROMPT_EN if language == "en" else EXPLAIN_PROMPT_TR
        messages = [
            ChatMessage(role="system", content=explain_prompt),
            ChatMessage(
                role="user",
                content=(
                    f"{'QUESTION' if language == 'en' else 'SORU'}:\n{question}\n\n"
                    f"DATA:\n{_compact(facts)}"
                ),
            ),
        ]
        try:
            narrative = ai_router.chat(
                db,
                messages,
                task_type=str(AITaskType.ANALYSIS),
                agent_kind=self.kind,
                user_id=user_id,
                conversation_id=conversation_id,
                max_tokens=1200,
                temperature=0.2,
            )
        except AIBudgetExceededError:
            narrative, error_key = None, "ai.budget_exceeded"
        except AIProviderError as exc:
            narrative, error_key = None, exc.message_key

        return self.build_result(
            data_context=facts,
            result=narrative,
            error_key=error_key,
            confidence=0.75 if query_result["row_count"] else 0.3,
        )


def _compact(facts: dict[str, Any]) -> str:
    from app.core.utils import dumps

    return dumps(facts, indent=1)


__all__ = [
    "DataAnalystAgent",
    "EXPLAIN_PROMPT_EN",
    "EXPLAIN_PROMPT_TR",
    "SYSTEM_PROMPT_EN",
    "SYSTEM_PROMPT_TR",
    "extract_sql",
]
