"""
Business agents.

Each agent owns one decision domain, gathers its own real figures, and carries a
bilingual system prompt (``SYSTEM_PROMPT_TR`` / ``SYSTEM_PROMPT_EN``).  The
orchestrator picks between them; everything else in the application talks to
:mod:`app.services.ai_service` rather than importing an agent directly.
"""

from __future__ import annotations

from app.ai.agents.base_agent import AgentResult, BaseAgent
from app.ai.agents.collection_risk_agent import CollectionRiskAgent
from app.ai.agents.data_analyst_agent import DataAnalystAgent
from app.ai.agents.forecast_agent import ForecastAgent
from app.ai.agents.inventory_agent import InventoryAgent
from app.ai.agents.orchestrator import AGENTS, OrchestratorAgent
from app.ai.agents.reporting_agent import ReportingAgent
from app.ai.agents.route_agent import RouteAgent
from app.ai.agents.sales_agent import SalesAgent

#: Every agent, including the orchestrator, keyed by :class:`AIAgentKind`.
ALL_AGENTS: dict[str, type[BaseAgent]] = {
    **AGENTS,
    OrchestratorAgent.kind: OrchestratorAgent,
}


def get_agent(kind: str) -> BaseAgent:
    """Instantiate the agent for *kind*, defaulting to the orchestrator."""
    cls = ALL_AGENTS.get(str(kind).upper(), OrchestratorAgent)
    return cls()


__all__ = [
    "AGENTS",
    "ALL_AGENTS",
    "AgentResult",
    "BaseAgent",
    "CollectionRiskAgent",
    "DataAnalystAgent",
    "ForecastAgent",
    "InventoryAgent",
    "OrchestratorAgent",
    "ReportingAgent",
    "RouteAgent",
    "SalesAgent",
    "get_agent",
]
