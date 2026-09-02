"""Hermetic environment construction and secret redaction.

Two safety properties, both load-bearing (see docs/threat-model.md):

* The agent process starts from an **allowlist**, never the operator's shell. Developer and
  production credentials are not inherited; only the target's declared credential is
  injected.
* Every artifact written to disk is passed through redaction, so a credential that does
  reach the agent's environment cannot leak into a committed log.
"""

from __future__ import annotations

import os
import re

# Variables the agent genuinely needs to run builds and tests. Anything not listed is
# dropped -- notably every *_TOKEN, *_KEY, AWS_*, and CI secret in the operator's shell.
ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_PATH",
    "PYTHONPATH",
    "PYENV_ROOT",
    "JAVA_HOME",
    "GOPATH",
    "GOROOT",
    "CARGO_HOME",
    "RUSTUP_HOME",
)

# Names whose *values* are redacted from artifacts, and which are never forwarded.
_SECRET_NAME = re.compile(
    r"(?:API_?KEY|AUTH_?TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_?KEY|SESSION_?TOKEN)",
    re.IGNORECASE,
)

# Structural patterns for secrets that appear in output without a variable name.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"(?i)\b(?:bearer)\s+[A-Za-z0-9._\-]{20,}"),
]

REDACTED = "[REDACTED]"


def build_agent_env(
    provider_env: dict[str, str],
    config_dir: str | None,
    operator_env: dict[str, str] | None = None,
    extra_allow: tuple[str, ...] = (),
) -> dict[str, str]:
    """Minimal environment for the agent subprocess."""
    src = operator_env if operator_env is not None else dict(os.environ)
    env = {k: src[k] for k in (*ENV_ALLOWLIST, *extra_allow) if k in src}

    # Keep the benchmark's own configuration out of the agent's view.
    env["ACB_BENCHMARK"] = "1"
    # Disable auto-updates so a version change cannot occur mid-suite.
    env["DISABLE_AUTOUPDATER"] = "1"

    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir

    env.update(provider_env)
    return env


def secret_values(env: dict[str, str], minimum_length: int = 8) -> list[str]:
    """Values worth redacting from artifacts, longest first (so prefixes don't shadow)."""
    values = [
        v for k, v in env.items() if _SECRET_NAME.search(k) and v and len(v) >= minimum_length
    ]
    return sorted(set(values), key=len, reverse=True)


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Remove known secret values and secret-shaped strings from text."""
    if not text:
        return text
    for value in secrets or []:
        if value:
            text = text.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
