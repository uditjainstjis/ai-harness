"""Configuration: pramana.toml  <  environment  <  CLI flags.

The API credential is never read from a file. It comes from AI_API_KEY (the
organisers' variable) at runtime. Provider-specific variables are accepted as a
fallback for local development only.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

if sys.version_info >= (3, 11):
    import tomllib as _toml
else:  # pragma: no cover
    import tomli as _toml


# name -> (wire protocol, base url, default model)
PROVIDERS: Dict[str, Dict[str, str]] = {
    "openai": {"kind": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-5-mini"},
    "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5"},
    "gemini": {"kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash"},
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-oss-120b"},
    "groq": {"kind": "openai", "base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-120b"},
    "xai": {"kind": "openai", "base_url": "https://api.x.ai/v1", "model": "grok-code-fast-1"},
    "deepseek": {"kind": "openai", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "mistral": {"kind": "openai", "base_url": "https://api.mistral.ai/v1", "model": "devstral-medium-latest"},
    "together": {"kind": "openai", "base_url": "https://api.together.xyz/v1", "model": "openai/gpt-oss-120b"},
    "fireworks": {"kind": "openai", "base_url": "https://api.fireworks.ai/inference/v1", "model": "accounts/fireworks/models/gpt-oss-120b"},
    "cerebras": {"kind": "openai", "base_url": "https://api.cerebras.ai/v1", "model": "gpt-oss-120b"},
    "nvidia": {"kind": "openai", "base_url": "https://integrate.api.nvidia.com/v1", "model": "openai/gpt-oss-120b"},
    "huggingface": {"kind": "openai", "base_url": "https://router.huggingface.co/v1", "model": "openai/gpt-oss-120b"},
    "moonshot": {"kind": "openai", "base_url": "https://api.moonshot.ai/v1", "model": "kimi-k2-0905-preview"},
    "ollama": {"kind": "openai", "base_url": "http://localhost:11434/v1", "model": "gpt-oss:120b-cloud"},
    "claude-cli": {"kind": "claude-cli", "base_url": "", "model": "haiku"},
    "mock": {"kind": "mock", "base_url": "", "model": "mock"},
}

# Key prefixes that identify a provider unambiguously. Order matters: longest first.
KEY_PREFIXES = [
    ("sk-ant-", "anthropic"),
    ("sk-or-", "openrouter"),
    ("AIza", "gemini"),
    ("gsk_", "groq"),
    ("xai-", "xai"),
    ("nvapi-", "nvidia"),
    ("csk-", "cerebras"),
    ("hf_", "huggingface"),
    ("fw_", "fireworks"),
    ("tgp_", "together"),
    ("sk-", "openai"),
]

FALLBACK_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "together": "TOGETHER_API_KEY",
}


def detect_provider(key: str) -> Optional[str]:
    for prefix, name in KEY_PREFIXES:
        if key.startswith(prefix):
            return name
    return None


@dataclass
class ModelConfig:
    provider: str = "auto"
    name: str = ""
    base_url: str = ""
    temperature: float = 0.0
    max_output_tokens: int = 8192
    reasoning_effort: str = ""
    tool_mode: str = "auto"  # auto | native | text
    request_timeout_s: float = 300.0
    context_window: int = 128000


@dataclass
class AgentConfig:
    max_steps: int = 60
    max_attempts: int = 2
    max_gate_rejections: int = 3
    token_budget: int = 3_000_000  # hard ceiling across all attempts (input + output)
    command_timeout_s: int = 180
    verify_timeout_s: int = 300
    compact_at_tokens: int = 60000  # compact old tool output past this prompt size
    keep_recent_observations: int = 8
    review: bool = True  # independent reviewer pass on the final diff
    seed: int = 7


@dataclass
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    runs_dir: str = "runs"
    workspace_dir: str = "workspace"
    api_key: str = field(default="", repr=False)

    # resolved at load time
    resolved_provider: str = ""
    resolved_kind: str = ""
    config_path: Optional[str] = None

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.resolved_provider,
            "model": self.model.name,
            "base_url": self.model.base_url,
            "tool_mode": self.model.tool_mode,
            "temperature": self.model.temperature,
            "api_key": "set" if self.api_key else "missing",
        }


def _apply(dc: Any, values: Dict[str, Any]) -> None:
    names = {f.name: f for f in fields(dc)}
    for k, v in values.items():
        if k in names and v is not None and v != "":
            cur = getattr(dc, k)
            try:
                if isinstance(cur, bool):
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
                elif isinstance(cur, int) and not isinstance(cur, bool):
                    v = int(v)
                elif isinstance(cur, float):
                    v = float(v)
            except (TypeError, ValueError):
                continue
            setattr(dc, k, v)


def find_config_file() -> Optional[Path]:
    env = os.environ.get("PRAMANA_CONFIG")
    if env and Path(env).is_file():
        return Path(env)
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent):
        p = base / "pramana.toml"
        if p.is_file():
            return p
    return None


def load_config(overrides: Optional[Dict[str, Any]] = None) -> Config:
    cfg = Config()
    path = find_config_file()
    if path:
        with open(path, "rb") as fh:
            data = _toml.load(fh)
        _apply(cfg.model, data.get("model", {}))
        _apply(cfg.agent, data.get("agent", {}))
        for k in ("runs_dir", "workspace_dir"):
            if k in data.get("paths", {}):
                setattr(cfg, k, data["paths"][k])
        cfg.config_path = str(path)
        # relative paths are relative to the harness root, not the caller's cwd
        root = path.parent
        cfg.runs_dir = str((root / cfg.runs_dir).resolve()) if not os.path.isabs(cfg.runs_dir) else cfg.runs_dir
        cfg.workspace_dir = (
            str((root / cfg.workspace_dir).resolve()) if not os.path.isabs(cfg.workspace_dir) else cfg.workspace_dir
        )

    env = os.environ
    _apply(
        cfg.model,
        {
            "provider": env.get("AI_PROVIDER"),
            "name": env.get("AI_MODEL"),
            "base_url": env.get("AI_BASE_URL"),
            "temperature": env.get("AI_TEMPERATURE"),
            "tool_mode": env.get("PRAMANA_TOOL_MODE"),
            "reasoning_effort": env.get("AI_REASONING_EFFORT"),
        },
    )
    _apply(cfg.agent, {"max_steps": env.get("PRAMANA_MAX_STEPS"), "max_attempts": env.get("PRAMANA_MAX_ATTEMPTS")})
    if overrides:
        _apply(cfg.model, overrides.get("model", {}))
        _apply(cfg.agent, overrides.get("agent", {}))

    cfg.api_key = (env.get("AI_API_KEY") or "").strip()
    resolve_provider(cfg)
    return cfg


def resolve_provider(cfg: Config) -> None:
    prov = (cfg.model.provider or "auto").lower()
    if prov == "auto":
        detected = detect_provider(cfg.api_key) if cfg.api_key else None
        if detected:
            prov = detected
        elif cfg.model.base_url:
            prov = "openai-compatible"
        elif not cfg.api_key:
            # no key at all: try provider-specific fallbacks (local development convenience)
            for name, var in FALLBACK_KEY_ENVS.items():
                if os.environ.get(var):
                    prov, cfg.api_key = name, os.environ[var].strip()
                    break
            else:
                prov = "unconfigured"
        else:
            prov = "openai"  # an unrecognised key shape: OpenAI-compatible is the most common wire format
    if not cfg.api_key and prov in FALLBACK_KEY_ENVS and os.environ.get(FALLBACK_KEY_ENVS[prov]):
        cfg.api_key = os.environ[FALLBACK_KEY_ENVS[prov]].strip()

    spec = PROVIDERS.get(prov)
    if spec is None:
        spec = {"kind": "openai", "base_url": cfg.model.base_url, "model": cfg.model.name}
    cfg.resolved_provider = prov
    cfg.resolved_kind = spec["kind"]
    if not cfg.model.base_url:
        cfg.model.base_url = spec["base_url"]
    if not cfg.model.name:
        cfg.model.name = spec["model"]
