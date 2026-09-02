"""Adapter registry -- maps a target's `adapter` key to an implementation."""

from __future__ import annotations

from acb.adapters.anthropic import AnthropicAdapter
from acb.adapters.base import ProviderAdapter
from acb.adapters.openai_compat import OpenAICompatAdapter
from acb.models.target import TargetConfig

_ADAPTERS: dict[str, type[ProviderAdapter]] = {
    AnthropicAdapter.name: AnthropicAdapter,
    OpenAICompatAdapter.name: OpenAICompatAdapter,
}


def register_adapter(cls: type[ProviderAdapter]) -> type[ProviderAdapter]:
    _ADAPTERS[cls.name] = cls
    return cls


def available_adapters() -> list[str]:
    return sorted(_ADAPTERS)


def get_adapter(target: TargetConfig) -> ProviderAdapter:
    try:
        cls = _ADAPTERS[target.adapter]
    except KeyError:
        raise ValueError(
            f"target {target.id!r} requests unknown adapter {target.adapter!r}; "
            f"available: {', '.join(available_adapters())}"
        ) from None
    return cls(target)
