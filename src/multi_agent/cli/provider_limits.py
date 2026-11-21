"""Load local provider-limit settings at the first product turn."""

import math
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from multi_agent.adapters.provider_limits import ProviderBudget
from multi_agent.adapters.runtime_decode import strict_json_loads
from multi_agent.store.provider_limits import ProviderLedger, ProviderLimits

_PROVIDERS = {"codex", "claude"}
_COUNT_FIELDS = {"max_concurrent", "max_calls_per_day"}
_DURATION_FIELDS = {"max_turn_seconds", "max_seconds_per_day"}


def _configuration(path: Path) -> dict[str, ProviderLimits]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        value = strict_json_loads(raw)
    except ValueError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} must contain an object")
    result = {}
    for provider, settings in value.items():
        if provider not in _PROVIDERS:
            raise ValueError(f"{path.name}: unknown provider {provider}")
        if not isinstance(settings, dict):
            raise TypeError(f"{path.name}: {provider} must contain an object")
        for name, item in settings.items():
            field = f"{path.name}: {provider}.{name}"
            if name in _COUNT_FIELDS:
                if type(item) is not int or item <= 0:
                    raise ValueError(f"{field} must be a positive integer")
            elif name in _DURATION_FIELDS:
                if not _positive_duration(item):
                    raise ValueError(f"{field} must be finite positive seconds")
            else:
                raise ValueError(f"{field} is not a recognized limit")
        if settings:
            result[provider] = ProviderLimits(**settings)
    return result


def _positive_duration(value: Any) -> bool:
    try:
        return type(value) in (int, float) and value > 0 and math.isfinite(value)
    except OverflowError:
        return False


def provider_budget(data_dir: Path) -> Callable[[str], ProviderBudget | None]:
    directory = data_dir.expanduser().resolve()

    @cache
    def configured() -> dict[str, ProviderLimits]:
        return _configuration(directory / "provider-limits.json")

    def lookup(provider: str) -> ProviderBudget | None:
        limits = configured().get(provider)
        return None if limits is None else (ProviderLedger(directory), limits)

    return lookup
