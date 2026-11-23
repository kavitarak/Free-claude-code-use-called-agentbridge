"""Settings, provider presets and model routing. Standard library only."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    key_env: str | None  # None: no key needed (local server)
    base_env: str | None = None  # env var that overrides base_url
    local: bool = False


PROVIDERS: dict[str, Provider] = {
    p.name: p
    for p in [
        Provider("nvidia_nim", "https://integrate.api.nvidia.com/v1", "NVIDIA_NIM_API_KEY"),
        Provider("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        Provider("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
        Provider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
        Provider("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
        Provider("ollama", "http://localhost:11434/v1", None, "OLLAMA_BASE_URL", local=True),
        Provider("lmstudio", "http://localhost:1234/v1", None, "LMSTUDIO_BASE_URL", local=True),
        Provider("custom", "", "CUSTOM_API_KEY", "CUSTOM_BASE_URL"),
    ]
}

TIERS = ("opus", "sonnet", "haiku")


def home_dir() -> Path:
    return Path(os.environ.get("AGENTBRIDGE_HOME") or (Path.home() / ".agentbridge"))


def env_file() -> Path:
    return home_dir() / ".env"


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def file_is_private(path: Path) -> bool:
    """True when nobody but the owner can read the file (always True on Windows)."""
    if os.name == "nt" or not path.exists():
        return True
    return not (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def split_target(target: str) -> tuple[str, str]:
    """'openrouter/some/model:free' -> ('openrouter', 'some/model:free')."""
    provider, sep, model = target.partition("/")
    if not sep or not model:
        raise ConfigError(f"'{target}' must look like provider/model-id (providers: {', '.join(PROVIDERS)})")
    if provider not in PROVIDERS:
        raise ConfigError(f"unknown provider '{provider}' in '{target}' (providers: {', '.join(PROVIDERS)})")
    return provider, model


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8082
    auth_token: str = ""
    model: str = ""
    tier_models: dict[str, str] = field(default_factory=dict)
    fallbacks: list[str] = field(default_factory=list)
    max_concurrency: int = 4
    timeout: float = 120.0
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, m: dict[str, str]) -> "Settings":
        def num(name: str, default, cast):
            try:
                return cast(m.get(name) or default)
            except ValueError as e:
                raise ConfigError(f"{name} must be a number") from e

        s = cls(
            host=m.get("AGENTBRIDGE_HOST") or "127.0.0.1",
            port=num("AGENTBRIDGE_PORT", 8082, int),
            auth_token=m.get("AGENTBRIDGE_AUTH_TOKEN", ""),
            model=m.get("MODEL", ""),
            tier_models={t: m[f"MODEL_{t.upper()}"] for t in TIERS if m.get(f"MODEL_{t.upper()}")},
            fallbacks=[x.strip() for x in m.get("FALLBACK_MODELS", "").split(",") if x.strip()],
            max_concurrency=max(1, num("MAX_CONCURRENCY", 4, int)),
            timeout=num("REQUEST_TIMEOUT", 120, float),
            env=dict(m),
        )
        return s

    @classmethod
    def load(cls, environ: dict[str, str] | None = None, path: Path | None = None) -> "Settings":
        merged = parse_env_file(path or env_file())
        merged.update(os.environ if environ is None else environ)
        return cls.from_mapping(merged)

    # --- providers -------------------------------------------------------
    def base_url(self, provider: str) -> str:
        p = PROVIDERS[provider]
        url = (self.env.get(p.base_env) if p.base_env else "") or p.base_url
        return url.rstrip("/")

    def keys(self, provider: str) -> list[str]:
        p = PROVIDERS[provider]
        if not p.key_env:
            return [""]
        return [k.strip() for k in self.env.get(p.key_env, "").split(",") if k.strip()]

    def secrets(self) -> list[str]:
        """Every secret value we hold, so error text can be scrubbed."""
        out = [self.auth_token] if self.auth_token else []
        for p in PROVIDERS.values():
            if p.key_env:
                out += [k for k in self.keys(p.name) if len(k) >= 6]
        return out

    # --- routing ---------------------------------------------------------
    def targets(self) -> list[str]:
        t = [self.model] if self.model else []
        t += self.tier_models.values()
        t += self.fallbacks
        return t

    def validate(self) -> list[str]:
        """Return human-readable problems (empty list = fine)."""
        problems: list[str] = []
        if not self.targets():
            problems.append("MODEL is not set (example: MODEL=nvidia_nim/<model-id>)")
        for t in self.targets():
            try:
                provider, _ = split_target(t)
            except ConfigError as e:
                problems.append(str(e))
                continue
            p = PROVIDERS[provider]
            if p.key_env and not self.keys(provider):
                problems.append(f"{p.key_env} is not set (needed for {t})")
            if provider == "custom" and not self.base_url("custom"):
                problems.append("CUSTOM_BASE_URL is not set (needed for the custom provider)")
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.auth_token:
            problems.append("listening beyond localhost without AGENTBRIDGE_AUTH_TOKEN is unsafe")
        return problems

    def resolve(self, requested: str) -> list[str]:
        """Pick the ordered list of provider/model targets for a requested model name."""
        requested = requested or ""
        primary = ""
        head = requested.partition("/")[0]
        if head in PROVIDERS and "/" in requested:
            primary = requested  # client asked for an explicit provider/model
        else:
            low = requested.lower()
            tier = next((t for t in TIERS if t in low), None)
            primary = (self.tier_models.get(tier) if tier else "") or self.model
        if not primary:
            raise ConfigError("MODEL is not set. Run `agentbridge init` and edit ~/.agentbridge/.env")
        chain: list[str] = []
        for t in [primary, *self.fallbacks]:
            if t not in chain:
                chain.append(t)
        return chain
