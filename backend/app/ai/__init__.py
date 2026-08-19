"""
AI subsystem.

Layers, from the wire inwards:

``providers``      vendor clients (LM Studio, NVIDIA, Claude) behind one contract
``router``         provider selection, failover, token accounting, budget
``sql_guard``      read-only SQL policy for natural-language querying
``terminal_guard`` permission tiers and hard blocks for the development terminal
``data_context``   real business figures that agents are allowed to talk about
``agents``         one specialist per decision domain, all bilingual

Import ``app.services.ai_service`` rather than these modules from the API layer:
it owns persistence, auditing and conversation state.
"""

from __future__ import annotations

from app.ai.base import BaseProvider, ChatMessage, ChatResult, EmbeddingResult

__all__ = ["BaseProvider", "ChatMessage", "ChatResult", "EmbeddingResult"]
