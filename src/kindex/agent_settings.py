"""Per-client and per-instance Kindex behavior overrides."""

from __future__ import annotations

from typing import Any

from .agent_adapters import normalize_adapter


ALLOWED_AGENT_SETTING_KEYS = {
    # Attention cadence, budgets, and rendering.
    "attention.enabled",
    "attention.tick_interval",
    "attention.display",
    "attention.max_context_chars",
    "attention.cooldown_seconds",
    "attention.max_check_cost",
    "attention.max_conversation_cost",
    # Sim cadence, budgets, and rendering.
    "sim.enabled",
    "sim.backend",
    "sim.model",
    "sim.agent_model",
    "sim.agent_effort",
    "sim.ollama_url",
    "sim.ollama_model",
    "sim.max_conversation_reviews",
    "sim.max_daily_reviews",
    "sim.agent_timeout",
    "sim.budget_warning_fraction",
    "sim.tick_interval",
    "sim.threshold",
    "sim.display",
    "sim.max_review_cost",
    "sim.max_conversation_cost",
    # Collaboration prompt injection cadence.
    "collab.display",
    "collab.prompt_cooldown_minutes",
    # Hook-only behavior that is not part of the main Config model.
    "hooks.prime_tokens",
}

CONFIG_BACKED_AGENT_KEYS = {
    key for key in ALLOWED_AGENT_SETTING_KEYS
    if not key.startswith("hooks.")
}


def normalize_agent_client(client: str | None) -> str:
    return normalize_adapter(client or "")


def resolve_agent_instance_key(
    client: str | None,
    explicit: str | None = None,
    hook_payload: dict[str, Any] | None = None,
) -> str:
    """Resolve the instance/conversation override key for an agent."""
    canonical = normalize_agent_client(client)
    if explicit:
        value = explicit.strip()
        return value if value.startswith(f"{canonical}:") else f"{canonical}:{value}"

    payload = hook_payload or {}
    for key in (
        "agent_instance",
        "agentInstance",
        "conversation_id",
        "conversationId",
        "session_id",
        "sessionId",
        "chat_id",
        "chatId",
    ):
        value = payload.get(key)
        if value:
            return f"{canonical}:{value}"

    return ""


def _section_items(section: str, value: Any) -> list[tuple[str, Any]]:
    if not isinstance(value, dict):
        return []
    return [(f"{section}.{key}", val) for key, val in value.items()]


def flatten_agent_override(override: Any) -> dict[str, Any]:
    """Flatten an AgentOverrideConfig or dict to dot-path settings."""
    if not override:
        return {}
    if hasattr(override, "model_dump"):
        data = override.model_dump()
    elif isinstance(override, dict):
        data = dict(override)
    else:
        return {}

    flattened: dict[str, Any] = {}
    for section in ("attention", "sim", "collab", "hooks"):
        for key, value in _section_items(section, data.get(section)):
            flattened[key] = value
    return {
        key: value for key, value in flattened.items()
        if key in ALLOWED_AGENT_SETTING_KEYS
    }


def _assign_model_path(config: Any, key: str, value: Any) -> None:
    current = config
    parts = key.split(".")
    for part in parts[:-1]:
        current = getattr(current, part)
    setattr(current, parts[-1], value)


def _agent_overrides(config: Any, client: str, instance_key: str | None) -> list[dict[str, Any]]:
    agents = getattr(config, "agents", None)
    if not agents:
        return []

    overrides: list[dict[str, Any]] = []
    clients = getattr(agents, "clients", {}) or {}
    client_override = clients.get(client)
    if client_override:
        overrides.append(flatten_agent_override(client_override))

    if instance_key:
        instances = getattr(agents, "instances", {}) or {}
        instance_override = instances.get(instance_key)
        if instance_override:
            declared_client = getattr(instance_override, "client", "") or ""
            if not declared_client or normalize_agent_client(declared_client) == client:
                overrides.append(flatten_agent_override(instance_override))
    return overrides


def apply_agent_overrides(
    config: Any,
    *,
    client: str | None,
    instance_key: str | None = None,
) -> Any:
    """Return a deep-copied Config with client/instance behavior overrides."""
    canonical = normalize_agent_client(client)
    if not canonical or canonical == "plain":
        return config
    effective = config.model_copy(deep=True)
    for override in _agent_overrides(config, canonical, instance_key):
        for key, value in override.items():
            if key in CONFIG_BACKED_AGENT_KEYS:
                _assign_model_path(effective, key, value)
    return effective


def agent_setting_value(
    config: Any,
    *,
    client: str | None,
    instance_key: str | None = None,
    key: str,
    default: Any = None,
) -> Any:
    canonical = normalize_agent_client(client)
    value = default
    for override in _agent_overrides(config, canonical, instance_key):
        if key in override:
            value = override[key]
    return value


def _selected_effective_settings(config: Any) -> dict[str, Any]:
    return {
        "attention": {
            "enabled": config.attention.enabled,
            "tick_interval": config.attention.tick_interval,
            "display": config.attention.display,
            "max_context_chars": config.attention.max_context_chars,
            "cooldown_seconds": config.attention.cooldown_seconds,
            "max_check_cost": config.attention.max_check_cost,
            "max_conversation_cost": config.attention.max_conversation_cost,
        },
        "sim": {
            "enabled": config.sim.enabled,
            "backend": config.sim.backend,
            "model": config.sim.model,
            "agent_model": config.sim.agent_model,
            "agent_effort": config.sim.agent_effort,
            "ollama_url": config.sim.ollama_url,
            "ollama_model": config.sim.ollama_model,
            "max_conversation_reviews": config.sim.max_conversation_reviews,
            "max_daily_reviews": config.sim.max_daily_reviews,
            "agent_timeout": config.sim.agent_timeout,
            "budget_warning_fraction": config.sim.budget_warning_fraction,
            "tick_interval": config.sim.tick_interval,
            "threshold": config.sim.threshold,
            "display": config.sim.display,
            "max_review_cost": config.sim.max_review_cost,
            "max_conversation_cost": config.sim.max_conversation_cost,
        },
        "collab": {
            "display": config.collab.display,
            "prompt_cooldown_minutes": config.collab.prompt_cooldown_minutes,
        },
    }


def agent_settings_summary(
    config: Any,
    *,
    client: str,
    instance_key: str | None = None,
) -> dict[str, Any]:
    canonical = normalize_agent_client(client)
    agents = getattr(config, "agents", None)
    client_override = (getattr(agents, "clients", {}) or {}).get(canonical) if agents else None
    instance_override = (
        (getattr(agents, "instances", {}) or {}).get(instance_key)
        if agents and instance_key else None
    )
    effective = apply_agent_overrides(config, client=canonical, instance_key=instance_key)
    return {
        "client": canonical,
        "instance": instance_key or "",
        "allowed_keys": sorted(ALLOWED_AGENT_SETTING_KEYS),
        "client_overrides": flatten_agent_override(client_override),
        "instance_overrides": flatten_agent_override(instance_override),
        "effective": _selected_effective_settings(effective),
        "hook_values": {
            "prime_tokens": agent_setting_value(
                config,
                client=canonical,
                instance_key=instance_key,
                key="hooks.prime_tokens",
                default=750,
            ),
        },
    }


def validate_agent_setting_key(key: str) -> str:
    normalized = (key or "").strip()
    if normalized not in ALLOWED_AGENT_SETTING_KEYS:
        allowed = ", ".join(sorted(ALLOWED_AGENT_SETTING_KEYS))
        raise ValueError(f"Unsupported agent setting '{key}'. Allowed: {allowed}")
    return normalized


def agent_config_write_key(
    *,
    scope: str,
    client: str,
    instance_key: str,
    setting_key: str,
) -> str:
    """Map a behavior setting to its YAML config path."""
    setting = validate_agent_setting_key(setting_key)
    canonical = normalize_agent_client(client)
    if not canonical or canonical == "plain":
        raise ValueError("Agent config writes require --client")
    if "." in canonical:
        raise ValueError("Client names cannot contain dots")
    if scope == "client":
        return f"agents.clients.{canonical}.{setting}"
    if scope == "instance":
        if not instance_key:
            raise ValueError("Instance-scoped writes require --instance")
        if "." in instance_key:
            raise ValueError("Instance keys cannot contain dots")
        return f"agents.instances.{instance_key}.{setting}"
    raise ValueError("Scope must be 'client' or 'instance'")
