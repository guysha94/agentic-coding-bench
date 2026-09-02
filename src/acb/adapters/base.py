"""Provider/model adapters.

An adapter owns exactly two provider-specific concerns:

1. **Environment wiring** -- how Claude Code is pointed at this provider's endpoint and
   credential.
2. **Usage normalisation** -- translating the provider's usage payload into
   `NormalizedUsage` without double-counting cached or reasoning tokens.

Everything else (task definitions, isolation, validation, scoring, reporting) is
provider-agnostic. Adding a provider that speaks an existing dialect needs only YAML.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from acb.models.target import TargetConfig
from acb.models.usage import NORMALIZATION_VERSION, NormalizedUsage


class ProviderAdapter(ABC):
    """Base adapter. Subclasses declare a `dialect` and implement the two hooks."""

    name: str = "base"
    dialect: str = "unknown"

    def __init__(self, target: TargetConfig) -> None:
        self.target = target

    # ------------------------------------------------------------------ environment ----
    @abstractmethod
    def agent_env(self, operator_env: dict[str, str]) -> dict[str, str]:
        """Env vars that route Claude Code to this target's model.

        Receives the operator's environment (to resolve credentials by name) and returns
        ONLY the variables that should be added to the agent process. The caller starts
        from a minimal allowlist, so anything not returned here is absent.
        """

    def cli_args(self) -> list[str]:
        """Provider-specific CLI flags. Model selection is handled by the runner."""
        args: list[str] = []
        effort = self.target.model_params.get("effort")
        if effort:
            args += ["--effort", str(effort)]
        args += list(self.target.cli_args)
        return args

    def required_env_vars(self) -> list[str]:
        """Operator-side env vars that must be present. Used by `benchmark doctor`."""
        return [self.target.api_key_env] if self.target.api_key_env else []

    # ------------------------------------------------------------------ usage ----------
    @abstractmethod
    def normalize_usage(self, payloads: list[dict[str, Any]]) -> NormalizedUsage:
        """Fold per-message provider usage payloads into one NormalizedUsage."""

    # ------------------------------------------------------------------ helpers --------
    @staticmethod
    def _int(payload: dict[str, Any], *keys: str) -> int:
        """First present, non-null, integer-ish value among `keys`; 0 otherwise."""
        for k in keys:
            v = payload.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        return 0

    def _base_usage(self) -> NormalizedUsage:
        return NormalizedUsage(normalization_version=NORMALIZATION_VERSION, dialect=self.dialect)
