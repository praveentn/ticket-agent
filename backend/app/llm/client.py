"""Groq LLM gateway.

Every call walks the configured model chain (GROQ_MODEL_PRIMARY -> GROQ_MODEL_FALLBACKS -> GROQ_MODEL_FALLBACK)
and moves to the next model on API errors or invalid output. Callers get back which model answered and the
token usage so the workflow trace can show it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import groq
from groq import AsyncGroq
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.core.metrics import LLM_CALLS, LLM_TOKENS

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_client: AsyncGroq | None = None


def get_client() -> AsyncGroq:
    global _client
    if _client is None:
        _client = AsyncGroq(api_key=settings.groq_api_key, timeout=settings.llm_timeout_seconds, max_retries=1)
    return _client


class LLMUnavailable(RuntimeError):
    """Every model in the chain failed; callers fall back to the heuristic path."""


@dataclass
class LLMResult:
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    attempts: list[dict[str, str]] = field(default_factory=list)

    def usage(self, purpose: str) -> dict[str, Any]:
        return {"purpose": purpose, "model": self.model, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out}


def _extra_params(model: str) -> dict[str, Any]:
    # gpt-oss models are reasoning models; keep reasoning short for latency-sensitive support turns.
    if "gpt-oss" in model:
        return {"reasoning_effort": "low"}
    return {}


def _record(result: LLMResult, purpose: str, usage: Any) -> None:
    if usage is not None:
        result.tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        result.tokens_out = getattr(usage, "completion_tokens", 0) or 0
    LLM_CALLS.labels(purpose=purpose, model=result.model, outcome="ok").inc()
    LLM_TOKENS.labels(direction="in", model=result.model).inc(result.tokens_in)
    LLM_TOKENS.labels(direction="out", model=result.model).inc(result.tokens_out)


def _fail(attempts: list[dict[str, str]], purpose: str, model: str, exc: Exception) -> None:
    LLM_CALLS.labels(purpose=purpose, model=model, outcome="error").inc()
    attempts.append({"model": model, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    log.warning("llm call failed", extra={"purpose": purpose, "model": model, "error": str(exc)[:300]})


async def complete_json(
    *, system: str, messages: list[dict[str, str]], schema: type[T], purpose: str
) -> tuple[T, LLMResult]:
    """Structured output: JSON mode plus Pydantic validation, failing over across the model chain."""
    schema_hint = json.dumps(schema.model_json_schema(), separators=(",", ":"))
    full_system = (
        f"{system}\n\nRespond with a single JSON object only, no prose. It must validate against this JSON Schema:\n"
        f"{schema_hint}"
    )
    attempts: list[dict[str, str]] = []
    for model in settings.llm_model_chain:
        try:
            resp = await get_client().chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": full_system}, *messages],
                response_format={"type": "json_object"},
                max_tokens=settings.llm_max_tokens,
                **_extra_params(model),
            )
            content = resp.choices[0].message.content or ""
            parsed = schema.model_validate_json(content)
            result = LLMResult(model=model, attempts=attempts)
            _record(result, purpose, resp.usage)
            return parsed, result
        except (groq.APIError, ValidationError, json.JSONDecodeError, IndexError) as exc:
            _fail(attempts, purpose, model, exc)
    raise LLMUnavailable(json.dumps(attempts))


async def choose_tool(
    *, system: str, messages: list[dict[str, str]], tools: list[dict[str, Any]], purpose: str
) -> tuple[str | None, dict[str, Any], str, LLMResult]:
    """Let the model pick one of the approved tools. Returns (tool_name|None, args, assistant_text, result)."""
    attempts: list[dict[str, str]] = []
    for model in settings.llm_model_chain:
        try:
            resp = await get_client().chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, *messages],
                tools=tools,
                tool_choice="auto",
                max_tokens=settings.llm_max_tokens,
                **_extra_params(model),
            )
            msg = resp.choices[0].message
            result = LLMResult(model=model, attempts=attempts)
            _record(result, purpose, resp.usage)
            if msg.tool_calls:
                call = msg.tool_calls[0]
                args = json.loads(call.function.arguments or "{}")
                return call.function.name, args if isinstance(args, dict) else {}, msg.content or "", result
            return None, {}, msg.content or "", result
        except (groq.APIError, json.JSONDecodeError, IndexError) as exc:
            _fail(attempts, purpose, model, exc)
    raise LLMUnavailable(json.dumps(attempts))


async def stream_text(
    *, system: str, messages: list[dict[str, str]], on_token: Callable[[str], Awaitable[None] | None], purpose: str
) -> tuple[str, LLMResult]:
    """Stream a completion token by token. Fails over only if a model errors before emitting anything."""
    attempts: list[dict[str, str]] = []
    for model in settings.llm_model_chain:
        emitted = False
        parts: list[str] = []
        usage = None
        try:
            stream = await get_client().chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, *messages],
                max_tokens=settings.llm_max_tokens,
                stream=True,
                **_extra_params(model),
            )
            async for chunk in stream:
                x_groq = getattr(chunk, "x_groq", None)
                if x_groq is not None and getattr(x_groq, "usage", None) is not None:
                    usage = x_groq.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    parts.append(delta)
                    emitted = True
                    maybe = on_token(delta)
                    if maybe is not None:
                        await maybe
            text = "".join(parts).strip()
            if not text:
                raise ValueError("empty completion")
            result = LLMResult(model=model, attempts=attempts)
            _record(result, purpose, usage)
            return text, result
        except (groq.APIError, ValueError) as exc:
            _fail(attempts, purpose, model, exc)
            if emitted:  # the user already saw partial output; don't splice in a different model's answer
                raise LLMUnavailable(json.dumps(attempts)) from exc
    raise LLMUnavailable(json.dumps(attempts))
