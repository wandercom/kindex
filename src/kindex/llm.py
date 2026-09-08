"""Optional LLM integration for classification and smart search.

Falls back gracefully to keyword matching when LLM is unavailable or over budget.
"""

from __future__ import annotations

import json
import copy
import os
import sys
import urllib.error
import urllib.request
from types import SimpleNamespace

from .budget import BudgetLedger
from .config import Config
from .privacy import redact, redact_text
from .privacy import redacting_print as print


class _PrivateMessages:
    """Keep the provider SDK and operational credentials outside prompt data."""

    def __init__(self, messages):
        self._messages = messages

    def __getattr__(self, name):
        return getattr(self._messages, name)

    def create(self, **kwargs):
        response = self._messages.create(**redact(kwargs))
        blocks = getattr(response, "content", None)
        if isinstance(blocks, list):
            cleaned = []
            for block in blocks:
                if isinstance(block, dict):
                    cleaned.append(redact(block))
                elif isinstance(getattr(block, "text", None), str):
                    projected = copy.copy(block)
                    projected.text = redact_text(block.text)
                    cleaned.append(projected)
                else:
                    cleaned.append(block)
            response = copy.copy(response)
            response.content = cleaned
        return response


class _PrivateClient:
    def __init__(self, client):
        self._client = client
        self.messages = _PrivateMessages(client.messages)

    def __getattr__(self, name):
        return getattr(self._client, name)

# Authoritative pricing per token (cache-aware)
PRICING = {
    "claude-haiku-4-5-20251001": {
        "input": 1.00e-6, "output": 5.00e-6,
        "cache_write": 1.25e-6, "cache_read": 0.10e-6,
    },
    "claude-sonnet-4-6": {
        "input": 3.00e-6, "output": 15.00e-6,
        "cache_write": 3.75e-6, "cache_read": 0.30e-6,
    },
    "claude-opus-4-6": {
        "input": 5.00e-6, "output": 25.00e-6,
        "cache_write": 6.25e-6, "cache_read": 0.50e-6,
    },
    "gpt-5.4-nano": {
        "input": 0.20e-6, "output": 1.25e-6,
        "cache_write": 0.20e-6, "cache_read": 0.02e-6,
    },
    "gpt-5.4-mini": {
        "input": 0.75e-6, "output": 4.50e-6,
        "cache_write": 0.75e-6, "cache_read": 0.075e-6,
    },
    "gpt-5-nano": {
        "input": 0.05e-6, "output": 0.40e-6,
        "cache_write": 0.05e-6, "cache_read": 0.005e-6,
    },
}
_DEFAULT_PRICE = {
    "input": 1.00e-6, "output": 5.00e-6,
    "cache_write": 1.25e-6, "cache_read": 0.10e-6,
}


def _key_env_names(config: Config) -> list[str]:
    """Return configured API key env vars, supporting comma-separated fallback."""
    names: list[str] = []
    for chunk in str(config.llm.api_key_env or "").replace(";", ",").split(","):
        name = chunk.strip()
        if name and name not in names:
            names.append(name)
    return names


def resolve_api_key(config: Config) -> tuple[str | None, str]:
    """Resolve the first available configured API key env var."""
    for name in _key_env_names(config):
        value = os.environ.get(name)
        if value:
            return value, name
    return None, ", ".join(_key_env_names(config))


def is_configured(config: Config) -> bool:
    """Return True when the configured LLM provider can make calls."""
    if not config.llm.enabled:
        return False
    provider = config.llm.provider.lower()
    if provider not in {"anthropic", "openai"}:
        return False
    api_key, _ = resolve_api_key(config)
    return bool(api_key)


class _OpenAIResponsesMessages:
    """Small adapter that exposes the Anthropic-like messages.create shape."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def create(self, *, model: str, max_tokens: int, messages: list[dict]) -> SimpleNamespace:
        messages = redact(messages)
        payload = {
            "model": model,
            "input": [
                {
                    "role": message.get("role", "user"),
                    "content": message.get("content", ""),
                }
                for message in messages
            ],
            "max_output_tokens": max_tokens,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Provider response bodies can echo prompts or authorization data.
            # Status is sufficient for this adapter's fallback behavior.
            raise RuntimeError(f"OpenAI API error {exc.code}") from None

        usage = data.get("usage") or {}
        cached = _nested_usage_value(
            usage,
            ("input_tokens_details", "cached_tokens"),
            ("prompt_tokens_details", "cached_tokens"),
        )
        total_input = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        usage_obj = SimpleNamespace(
            input_tokens=max(0, total_input - cached),
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cached,
        )
        return SimpleNamespace(
            content=[SimpleNamespace(text=redact_text(_extract_openai_text(data)))],
            usage=usage_obj,
        )


class _OpenAIResponsesClient:
    def __init__(self, api_key: str):
        self.messages = _OpenAIResponsesMessages(api_key)


def _extract_openai_text(data: dict) -> str:
    text = data.get("output_text")
    if isinstance(text, str):
        return text
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            value = content.get("text")
            if isinstance(value, str):
                parts.append(value)
    if parts:
        return "\n".join(parts)
    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def _nested_usage_value(usage, *paths: tuple[str, ...]) -> int:
    for path in paths:
        current = usage
        for key in path:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                current = getattr(current, key, None)
            if current is None:
                break
        else:
            try:
                return int(current or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _usage_value(usage, *names: str) -> int:
    for name in names:
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def get_client(config: Config):
    """Get configured LLM client, or None if not available."""
    if not config.llm.enabled:
        return None
    api_key, key_env = resolve_api_key(config)
    if not api_key:
        print(
            f"Warning: LLM enabled but none of {key_env or 'llm.api_key_env'} are set. "
            "Falling back to keyword matching.",
            file=sys.stderr,
        )
        return None

    provider = config.llm.provider.lower()
    if provider == "openai":
        return _PrivateClient(_OpenAIResponsesClient(api_key))

    if provider != "anthropic":
        print(
            f"Warning: unsupported LLM provider '{config.llm.provider}'. "
            "Supported providers: anthropic, openai.",
            file=sys.stderr,
        )
        return None

    try:
        import anthropic
        return _PrivateClient(anthropic.Anthropic(api_key=api_key))
    except ImportError:
        print("Warning: LLM enabled but 'anthropic' package not installed. "
              "Install with: pip install kindex[llm]", file=sys.stderr)
        return None


# Backward-compatible alias
_get_client = get_client


def calculate_cost(model: str, usage) -> dict:
    """Calculate cost from response usage, cache-aware."""
    p = PRICING.get(model, _DEFAULT_PRICE)
    tokens_in = _usage_value(usage, "input_tokens", "prompt_tokens")
    tokens_out = _usage_value(usage, "output_tokens", "completion_tokens")
    cache_write = _usage_value(usage, "cache_creation_input_tokens")
    cache_read = _usage_value(usage, "cache_read_input_tokens")
    nested_cache_read = _nested_usage_value(
        usage,
        ("input_tokens_details", "cached_tokens"),
        ("prompt_tokens_details", "cached_tokens"),
    )
    if not cache_read:
        cache_read = nested_cache_read
    billable_input = max(0, tokens_in - nested_cache_read) if nested_cache_read else tokens_in
    amount = (
        billable_input * p["input"]
        + cache_write * p["cache_write"]
        + cache_read * p["cache_read"]
        + tokens_out * p["output"]
    )
    return {
        "amount": round(amount, 8),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_creation_tokens": cache_write,
        "cache_read_tokens": cache_read,
    }


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Legacy cost estimation (no cache awareness)."""
    pricing = PRICING.get(model, _DEFAULT_PRICE)
    return tokens_in * pricing["input"] + tokens_out * pricing["output"]


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate LLM cost before a call."""
    return _estimate_cost(model, tokens_in, tokens_out)


def classify_for_graph(
    text: str,
    existing_slugs: list[str],
    config: Config,
    ledger: BudgetLedger,
) -> dict | None:
    """Ask LLM to classify text into relevant topics/skills.

    Returns dict with keys: topics, skills, suggested_title, suggested_tags
    Returns None if LLM unavailable or over budget.
    """
    if not ledger.can_spend():
        return None

    client = _get_client(config)
    if client is None:
        return None

    slugs_str = ", ".join(existing_slugs[:50])
    prompt = f"""Given this information from a conversation:

"{text}"

And these existing knowledge graph nodes: {slugs_str}

Respond with ONLY a YAML block:
```yaml
related_topics: [list of existing slugs that relate, max 5]
new_topic_slug: suggested-slug-if-new  # or empty string if fits existing
suggested_title: "Short title"
suggested_tags: [tag1, tag2]
is_skill: false  # true if this describes an ability/capability
```"""

    try:
        response = client.messages.create(
            model=config.llm.model,
            max_tokens=200,
            messages=[{"role": "user", "content": redact_text(prompt)}],
        )

        tokens_in = response.usage.input_tokens
        tokens_out = response.usage.output_tokens
        cost = _estimate_cost(config.llm.model, tokens_in, tokens_out)
        ledger.record(cost, model=config.llm.model, purpose="classify",
                      tokens_in=tokens_in, tokens_out=tokens_out)

        # Parse YAML from response
        import yaml
        text_out = response.content[0].text
        # Extract yaml block
        if "```yaml" in text_out:
            text_out = text_out.split("```yaml")[1].split("```")[0]
        elif "```" in text_out:
            text_out = text_out.split("```")[1].split("```")[0]

        return yaml.safe_load(text_out)
    except Exception:
        return None


def smart_search(
    query: str,
    existing_slugs: list[str],
    config: Config,
    ledger: BudgetLedger,
) -> list[str] | None:
    """Ask LLM to pick the most relevant slugs for a query.

    Returns list of slugs, or None if LLM unavailable.
    """
    if not ledger.can_spend():
        return None

    client = _get_client(config)
    if client is None:
        return None

    slugs_str = ", ".join(existing_slugs)
    prompt = f"""From these knowledge graph nodes: {slugs_str}

Which are most relevant to this query: "{query}"

Respond with ONLY a comma-separated list of slugs, most relevant first. Max 10."""

    try:
        response = client.messages.create(
            model=config.llm.model,
            max_tokens=150,
            messages=[{"role": "user", "content": redact_text(prompt)}],
        )

        tokens_in = response.usage.input_tokens
        tokens_out = response.usage.output_tokens
        cost = _estimate_cost(config.llm.model, tokens_in, tokens_out)
        ledger.record(cost, model=config.llm.model, purpose="search",
                      tokens_in=tokens_in, tokens_out=tokens_out)

        text_out = response.content[0].text.strip()
        slugs = [s.strip() for s in text_out.split(",")]
        return [s for s in slugs if s in existing_slugs]
    except Exception:
        return None
