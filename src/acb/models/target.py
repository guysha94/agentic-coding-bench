"""Benchmark target configuration.

A target is the experiment's controlled variable:

    target = model x provider x deployment x pricing model

Targets are pure configuration. Adding a provider, model, endpoint or price must never
require editing a benchmark task, and requires editing code only when the provider speaks a
genuinely new *usage dialect* (see `acb.adapters`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from acb.models.pricing import PricingConfig, UnknownPricing

DeploymentType = Literal["api", "self_hosted", "subscription", "local"]


class TargetConfig(BaseModel):
    """One concrete, runnable combination of model + provider + deployment + pricing."""

    id: str = Field(description="Stable key, e.g. 'glm-fireworks'. Used on the CLI.")

    # `model` is the identifier passed to the CLI/provider. Deliberately a free string:
    # provider model ids change often and must never be hardcoded or validated against a
    # closed list. `model_family` is the analysis-level grouping (e.g. 'glm').
    model: str
    model_family: str | None = None
    display_name: str | None = None

    provider: str
    deployment_type: DeploymentType = "api"
    adapter: str = Field(
        default="anthropic",
        description="Usage dialect / env wiring. One of the keys in the adapter registry.",
    )
    endpoint: str | None = None

    # --- credentials & environment ------------------------------------------------------
    api_key_env: str | None = Field(
        default=None,
        description="Name of the env var on the OPERATOR's machine holding the credential. "
        "Its value is injected into the run under the adapter's expected variable name. "
        "The secret itself is never written to config or artifacts.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra env vars for the agent process. Values starting with 'env:' are "
        "resolved from the operator's environment at run time.",
    )
    model_params: dict[str, Any] = Field(
        default_factory=dict,
        description="Model knobs recorded for reproducibility and applied where the CLI "
        "supports them (e.g. {'effort': 'high'}).",
    )
    cli_args: list[str] = Field(
        default_factory=list, description="Extra `claude` flags appended verbatim."
    )

    # --- isolation ----------------------------------------------------------------------
    isolate_config_dir: bool = Field(
        default=True,
        description="Give the run a scratch CLAUDE_CONFIG_DIR. Verified to strip ambient "
        "tools/MCP/plugins, but ALSO drops subscription (OAuth) auth -- set False for "
        "subscription targets, accepting the recorded reproducibility caveat.",
    )

    pricing: PricingConfig = Field(default_factory=UnknownPricing)

    enabled: bool = True
    notes: str | None = None

    @model_validator(mode="after")
    def _defaults(self) -> TargetConfig:
        if self.model_family is None:
            self.model_family = self.model.split("/")[-1].split(":")[0]
        if self.display_name is None:
            self.display_name = f"{self.model_family} / {self.provider}"
        if self.deployment_type == "self_hosted" and not self.endpoint:
            raise ValueError(f"target {self.id}: self_hosted deployment requires an endpoint")
        return self

    def resolve_env(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        """Resolve `env:` indirections against the operator's environment."""
        src = environ if environ is not None else dict(os.environ)
        out: dict[str, str] = {}
        for k, v in self.env.items():
            if v.startswith("env:"):
                name = v[4:]
                if name in src:
                    out[k] = src[name]
            else:
                out[k] = v
        return out

    def credential_available(self, environ: dict[str, str] | None = None) -> bool:
        src = environ if environ is not None else dict(os.environ)
        if self.api_key_env is None:
            # No declared credential: only viable when the CLI can use ambient auth, which
            # requires opting out of config-dir isolation.
            return not self.isolate_config_dir
        return bool(src.get(self.api_key_env))

    def pricing_snapshot(self) -> dict[str, Any]:
        """Frozen record of how this run should be priced, forever."""
        from acb.models.usage import NORMALIZATION_VERSION

        return {
            "target": self.id,
            "model": self.model,
            "model_family": self.model_family,
            "provider": self.provider,
            "deployment_type": self.deployment_type,
            "adapter": self.adapter,
            "endpoint": self.endpoint,
            "pricing_type": self.pricing.type,
            "currency": self.pricing.currency,
            "pricing_effective_date": self.pricing.effective_date,
            "pricing_snapshot": self.pricing.model_dump(mode="json"),
            "normalization_version": NORMALIZATION_VERSION,
        }


class TargetsFile(BaseModel):
    targets: dict[str, TargetConfig]

    @model_validator(mode="after")
    def _ids(self) -> TargetsFile:
        for key, t in self.targets.items():
            if t.id and t.id != key:
                raise ValueError(f"target key {key!r} does not match its id {t.id!r}")
            t.id = key
        return self


def load_targets(path: Path) -> dict[str, TargetConfig]:
    raw = yaml.safe_load(path.read_text()) or {}
    # Allow targets to omit `id`; the mapping key is authoritative.
    for key, value in (raw.get("targets") or {}).items():
        value.setdefault("id", key)
    return TargetsFile.model_validate(raw).targets
