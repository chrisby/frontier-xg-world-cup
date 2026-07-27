"""Unified reasoning-model client.

Defaults to OpenAI GPT-5.5 (config.REASONING_PROVIDER / REASONING_MODEL).
The Anthropic/Fable 5 code path is kept behind this same interface — set
REASONING_PROVIDER=anthropic in .env to switch back if Fable 5 returns.
"""
import json

from . import config

_MODELS = {"openai": "gpt-5.5-pro", "anthropic": "claude-opus-4-8", "sonnet5": "claude-sonnet-5", "fable5": "claude-fable-5"}
_MODEL_LABELS = {"openai": "GPT-5.5 Pro", "anthropic": "Claude Opus 4.8", "sonnet5": "Claude Sonnet 5", "fable5": "Claude Fable 5"}


def model_label(provider: str = None) -> str:
    """Human-readable name of the currently configured reasoning model."""
    p = provider or config.REASONING_PROVIDER
    return _MODEL_LABELS.get(p, config.REASONING_MODEL)


def complete_json(system: str, user: str, schema: dict, max_tokens: int = 8000,
                   effort: str = "high", provider: str = None) -> dict:
    """Ask the reasoning model for a JSON object matching `schema`."""
    p = provider or config.REASONING_PROVIDER
    if p in ("anthropic", "sonnet5", "fable5"):
        return _anthropic_json(system, user, schema, max_tokens, effort, model=_MODELS[p])
    return _openai_json(system, user, schema, max_tokens, effort)


def complete_text(system: str, user: str, max_tokens: int = 4000,
                   effort: str = "high", provider: str = None) -> str:
    """Ask the reasoning model for a free-text response."""
    p = provider or config.REASONING_PROVIDER
    if p in ("anthropic", "sonnet5", "fable5"):
        return _anthropic_text(system, user, max_tokens, effort, model=_MODELS[p])
    return _openai_text(system, user, max_tokens, effort)


def _anthropic_json(system, user, schema, max_tokens, effort, model=None):
    import anthropic
    resp = anthropic.Anthropic().messages.create(
        model=model or _MODELS["anthropic"], max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": effort,
                       "format": {"type": "json_schema", "schema": schema}},
        system=system,
        messages=[{"role": "user", "content": user}])
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def _anthropic_text(system, user, max_tokens, effort, model=None):
    import anthropic
    resp = anthropic.Anthropic().messages.create(
        model=model or _MODELS["anthropic"], max_tokens=max_tokens,
        thinking={"type": "adaptive"}, output_config={"effort": effort},
        system=system,
        messages=[{"role": "user", "content": user}])
    return next(b.text for b in resp.content if b.type == "text")


# gpt-5.5-pro only supports medium/high/xhigh reasoning effort (no low/minimal).
_OPENAI_EFFORT = {"low": "medium", "medium": "medium", "high": "high", "max": "xhigh"}


def _openai_json(system, user, schema, max_tokens, effort):
    import openai
    resp = openai.OpenAI().responses.create(
        model=_MODELS["openai"],
        reasoning={"effort": _OPENAI_EFFORT.get(effort, "medium")},
        instructions=system,
        input=user,
        text={"format": {"type": "json_schema", "name": "response",
                         "strict": True, "schema": schema}},
    )
    return json.loads(resp.output_text)


def _openai_text(system, user, max_tokens, effort):
    import openai
    resp = openai.OpenAI().responses.create(
        model=_MODELS["openai"],
        reasoning={"effort": _OPENAI_EFFORT.get(effort, "medium")},
        instructions=system,
        input=user,
    )
    return resp.output_text
