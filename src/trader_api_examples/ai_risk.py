from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, cast

import httpx

RiskAction = Literal["NORMAL", "REDUCE", "PAUSE"]


@dataclass(frozen=True)
class AccountContext:
    """Sanitized, point-in-time context supplied to the replay risk agent."""

    environment: str
    contract: str
    signal: str
    bar_time_ms: int
    close: float
    fast_average: float
    slow_average: float
    atr: float
    spread_bps: float
    equity: float
    daily_pnl_pct: float
    drawdown_pct: float
    open_positions: int
    market_data_fresh: bool


@dataclass(frozen=True)
class RiskDecision:
    action: RiskAction
    reason: str
    confidence: float | None = None


class RiskAgent(Protocol):
    def decide(self, context: AccountContext) -> RiskDecision:
        """Return one bounded action for the supplied point-in-time context."""


class RiskAgentError(ValueError):
    """Raised when the external risk agent cannot produce a valid decision."""


SYSTEM_PROMPT = """You are a bounded risk classifier used during a historical trading replay.
Return exactly one JSON object with keys action, reason, and confidence.
action must be exactly NORMAL, REDUCE, or PAUSE.
NORMAL permits the configured amount, REDUCE permits a smaller configured amount, and PAUSE
blocks the pending entry. Do not change strategy parameters, stops, limits, prices, or exits.
Use only the supplied point-in-time context. Do not infer or request future prices.
Keep reason under 200 characters and confidence between 0 and 1, or null if unavailable.
"""

_DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["NORMAL", "REDUCE", "PAUSE"]},
        "reason": {"type": "string", "maxLength": 200},
        "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
    },
    "required": ["action", "reason", "confidence"],
}


class OpenAIRiskAgent:
    """OpenAI Responses API adapter for the replay-only bounded risk decision."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        endpoint: str,
        timeout_seconds: float,
    ) -> None:
        if not api_key:
            raise RiskAgentError("OPENAI_API_KEY is required when --ai-risk-agent is enabled.")
        if not model:
            raise RiskAgentError("OPENAI_MODEL is required when --ai-risk-agent is enabled.")
        if not endpoint.startswith("https://"):
            raise RiskAgentError("AI risk endpoint must use HTTPS.")
        if timeout_seconds <= 0:
            raise RiskAgentError("AI risk timeout must be greater than zero.")
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds

    def decide(self, context: AccountContext) -> RiskDecision:
        payload = {
            "model": self._model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(asdict(context), sort_keys=True),
                        }
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "bounded_risk_decision",
                    "strict": True,
                    "schema": _DECISION_SCHEMA,
                }
            },
        }
        try:
            response = httpx.post(
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            response_payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RiskAgentError("AI risk request failed.") from error

        text = _response_text(response_payload)
        try:
            decision_payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise RiskAgentError("AI risk response was not valid JSON.") from error
        return _parse_decision(decision_payload)


def _response_text(payload: object) -> str:
    if isinstance(payload, dict):
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        return text.strip()
    raise RiskAgentError("AI risk response did not contain output text.")


def _parse_decision(payload: object) -> RiskDecision:
    if not isinstance(payload, dict):
        raise RiskAgentError("AI risk response must be a JSON object.")
    action = str(payload.get("action", "")).upper()
    if action not in {"NORMAL", "REDUCE", "PAUSE"}:
        raise RiskAgentError("AI risk response contained an invalid action.")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise RiskAgentError("AI risk response must include a reason.")
    confidence_value = payload.get("confidence")
    confidence: float | None
    if confidence_value is None:
        confidence = None
    elif isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float)):
        raise RiskAgentError("AI risk confidence must be a number or null.")
    else:
        confidence = float(confidence_value)
        if not 0.0 <= confidence <= 1.0:
            raise RiskAgentError("AI risk confidence must be between zero and one.")
    return RiskDecision(
        action=cast(RiskAction, action),
        reason=" ".join(reason.split())[:200],
        confidence=confidence,
    )
