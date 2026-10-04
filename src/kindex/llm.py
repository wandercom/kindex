"""Optional LLM integration for classification and smart search.

Falls back gracefully to keyword matching when LLM is unavailable or over budget.
"""

from __future__ import annotations

import json
import copy
import os
import re
import sys
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

from .budget import BudgetLedger
from .config import Config
from .privacy import redact, redact_text
from .privacy import redacting_print as print


# Thinking counts toward max_tokens. A model that thinks by default (Claude
# Opus 5.5 always does; Claude Opus 5, Claude Sonnet 5 and 5.5 and the Fable
# and Mythos models do unless told otherwise) can spend a cap sized for the
# visible reply alone before writing any text block.
_THINKS_BY_DEFAULT = re.compile(r"claude-(?:opus|sonnet)-5|claude-(?:fable|mythos)-")
THINKING_HEADROOM_TOKENS = 8000


def max_tokens_for(model: str, reply_tokens: int) -> int:
    """``max_tokens`` for a reply of about ``reply_tokens`` on ``model``."""
    if _THINKS_BY_DEFAULT.search(str(model or "").lower()):
        return reply_tokens + THINKING_HEADROOM_TOKENS
    return reply_tokens


class _PrivateMessages:
    """Keep the provider SDK and operational credentials outside prompt data."""

    def __init__(self, messages):
        self._messages = messages

    def __getattr__(self, name):
        return getattr(self._messages, name)

    def create(self, **kwargs):
        if isinstance(kwargs.get("max_tokens"), int):
            kwargs["max_tokens"] = max_tokens_for(kwargs.get("model", ""), kwargs["max_tokens"])
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
    "claude-haiku-4-5": {
        "input": 1.00e-6, "output": 5.00e-6,
        "cache_write": 1.25e-6, "cache_read": 0.10e-6,
    },
    "claude-sonnet-5": {
        "input": 2.00e-6, "output": 10.00e-6,
        "cache_write": 2.50e-6, "cache_read": 0.20e-6,
    },
    "claude-sonnet-5-5": {
        "input": 2.00e-6, "output": 10.00e-6,
        "cache_write": 2.50e-6, "cache_read": 0.20e-6,
    },
    "claude-opus-4-8": {
        "input": 5.00e-6, "output": 25.00e-6,
        "cache_write": 6.25e-6, "cache_read": 0.50e-6,
    },
    "claude-opus-5": {
        "input": 5.00e-6, "output": 25.00e-6,
        "cache_write": 6.25e-6, "cache_read": 0.50e-6,
    },
    "claude-opus-5-5": {
        "input": 4.00e-6, "output": 20.00e-6,
        "cache_write": 5.00e-6, "cache_read": 0.20e-6,
    },
    "claude-fable-5-1": {
        "input": 10.00e-6, "output": 50.00e-6,
        "cache_write": 12.50e-6, "cache_read": 0.25e-6,
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
# An unpriced model is costed at the most expensive known rate, so a budget cap
# can only over-count it: a new model must never slip past the cap at a cheap
# fallback price.
_DEFAULT_PRICE = max(PRICING.values(), key=lambda price: price["output"])
_PLATFORM_PREFIX = re.compile(r"^(?:[a-z]{2,4}\.)?anthropic\.")
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def price_for(model: str) -> dict:
    """Per-token prices for ``model``: exact ID, then the ID without a platform
    prefix (``us.anthropic.``) or date suffix, then the conservative default."""
    name = str(model or "")
    if name in PRICING:
        return PRICING[name]
    bare = _DATE_SUFFIX.sub("", _PLATFORM_PREFIX.sub("", name.lower()))
    return PRICING.get(bare, _DEFAULT_PRICE)


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

    def __init__(self, api_key: str, timeout: float = 30, retries: int = 0,
                 deadline: float | None = None):
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.deadline = deadline

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict],
        system: str | None = None,
        reasoning_effort: str | None = None,
        json_schema: dict | None = None,
        sample: int = 0,
    ) -> SimpleNamespace:
        """``system`` becomes the request's instructions; ``reasoning_effort``
        sets a reasoning model's effort; ``json_schema`` asks for structured
        output ({"name": ..., "schema": ...}). ``sample`` numbers independent
        samples of the same request: the provider samples each call
        independently, so it is not sent, but callers that cache responses
        key on it."""
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
        if system:
            payload["instructions"] = redact_text(system)
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        if json_schema:
            payload["text"] = {"format": {"type": "json_schema", "name": json_schema["name"],
                                          "schema": json_schema["schema"], "strict": True}}
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        # Rate limits and server errors are transient: a caller that asked for
        # retries gets them with backoff, all inside its deadline when it set one.
        stop = None if self.deadline is None else time.monotonic() + self.deadline
        for attempt in range(self.retries + 1):
            wait = self.timeout
            if stop is not None:
                wait = min(wait, stop - time.monotonic())
                if wait <= 0:
                    raise RuntimeError("OpenAI API deadline exceeded")
            try:
                with urllib.request.urlopen(request, timeout=wait) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                pause = 5 * (attempt + 1)
                if ((exc.code == 429 or exc.code >= 500) and attempt < self.retries
                        and (stop is None or time.monotonic() + pause < stop)):
                    time.sleep(pause)
                    continue
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
    def __init__(self, api_key: str, timeout: float = 30, retries: int = 0,
                 deadline: float | None = None):
        self.messages = _OpenAIResponsesMessages(api_key, timeout, retries, deadline)


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


def response_text(response) -> str:
    """The reply text of a messages response: its ``text`` blocks, joined.

    A model that thinks by default (Claude Opus 5 and 5.5, Claude Sonnet 5 and
    5.5) can return ``thinking`` blocks before the answer, and a refusal can
    return no text at all, so ``content[0]`` is not the reply. Blocks without a
    ``type`` (the OpenAI adapter's) count as text.
    """
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if isinstance(block, dict):
            kind, text = block.get("type", "text"), block.get("text")
        else:
            kind, text = getattr(block, "type", "text"), getattr(block, "text", None)
        if kind == "text" and isinstance(text, str):
            parts.append(text)
    return "".join(parts)


# A request with no deadline waited up to the SDK default of ten minutes with
# two retries. Commands get a minute; a hook passes its own budget, retries
# nothing, and so finishes (and records its spend) before the host kills it.
DEFAULT_LLM_TIMEOUT_SECONDS = 60.0


def get_client(config: Config, timeout: float | None = None, retries: int | None = None,
               deadline: float | None = None):
    """Get configured LLM client, or None if not available. ``timeout`` bounds
    each request. ``retries`` is how many times a rate-limited or failed
    request is retried; a caller that passes a timeout (a hook) gets none
    unless it asks, and the OpenAI client retries only when asked. ``deadline`` bounds one call, retries and backoff
    included; it defaults to the timeout when there are no retries."""
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
        retries = retries or 0
        return _PrivateClient(_OpenAIResponsesClient(
            api_key, timeout if timeout is not None else 30, retries,
            deadline if deadline is not None else (timeout if not retries else None)))
    if retries is None:
        retries = 0 if timeout is not None else 2

    if provider != "anthropic":
        print(
            f"Warning: unsupported LLM provider '{config.llm.provider}'. "
            "Supported providers: anthropic, openai.",
            file=sys.stderr,
        )
        return None

    try:
        import anthropic
        return _PrivateClient(anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout if timeout is not None else DEFAULT_LLM_TIMEOUT_SECONDS,
            max_retries=retries,
        ))
    except ImportError:
        print("Warning: LLM enabled but 'anthropic' package not installed. "
              "Install with: pip install kindex[llm]", file=sys.stderr)
        return None


# Backward-compatible alias
_get_client = get_client


def calculate_cost(model: str, usage) -> dict:
    """Calculate cost from response usage, cache-aware."""
    p = price_for(model)
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
    pricing = price_for(model)
    return tokens_in * pricing["input"] + tokens_out * pricing["output"]


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate LLM cost before a call."""
    return _estimate_cost(model, tokens_in, tokens_out)


# A prompt prefix shorter than the model's minimum is not cached even with a
# cache_control marker (no error; cache_creation_input_tokens stays 0). The
# minimum is not monotonic across generations. First match wins.
_MIN_CACHEABLE_TOKENS = (
    ("claude-sonnet-5-5", 512),
    ("claude-opus-5", 512),        # claude-opus-5 and claude-opus-5-5
    ("claude-fable-", 512),
    ("claude-mythos-5", 512),
    ("claude-sonnet-5", 1024),
    ("claude-opus-4-8", 1024),
    ("claude-sonnet-4", 1024),     # Sonnet 4.6, 4.5 and 4
    ("claude-opus-4-1", 1024),
    ("claude-opus-4-0", 1024),
    ("claude-opus-4-2025", 1024),
    ("claude-opus-4-7", 2048),
    ("claude-opus-4-6", 4096),
    ("claude-opus-4-5", 4096),
    ("claude-haiku-4-5", 4096),
)


def min_cacheable_tokens(model: str) -> int:
    """The smallest prompt prefix ``model`` will cache, in tokens."""
    name = str(model or "").lower()
    for prefix, minimum in _MIN_CACHEABLE_TOKENS:
        if prefix in name:
            return minimum
    return 2048


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
        text_out = response_text(response)
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

        text_out = response_text(response).strip()
        slugs = [s.strip() for s in text_out.split(",")]
        return [s for s in slugs if s in existing_slugs]
    except Exception:
        return None
