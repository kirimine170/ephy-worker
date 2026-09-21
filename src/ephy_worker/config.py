"""Explicit service configuration，with credentials resolved only at connection time．"""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, SecretStr, ValidationError, model_validator

from .schema import Limits, StrictModel


class ConfigurationError(ValueError):
    pass


def service_url(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not url.port
            and value.endswith(":")
        ):
            raise ValueError
        _ = url.port
    except ValueError:
        raise ConfigurationError(
            "service URL must be HTTP(S) without credentials，query or fragment"
        ) from None
    return value.rstrip("/")


def manager_service_url(value: str, *, allow_remote_api: bool, allow_insecure_http: bool) -> str:
    url = service_url(value)
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(hostname)
        loopback = address.is_loopback
        private = address.is_private
    except ValueError:
        loopback = hostname == "localhost"
        private = loopback or "." not in hostname or hostname.endswith((".local", ".lan", ".internal"))
    if not private and not allow_remote_api:
        raise ConfigurationError("public manager endpoint requires explicit allow_remote_api: true")
    if parsed.scheme == "http" and not loopback and not allow_insecure_http:
        raise ConfigurationError(
            "non-loopback manager endpoint requires HTTPS，a loopback tunnel，"
            "or explicit allow_insecure_http: true"
        )
    return url


def _resolve(value: str | None, env: str | None, label: str) -> str:
    resolved = os.environ.get(env, "") if env else (value or "")
    if not resolved.strip():
        raise ConfigurationError(f"{label} is not configured" + (f" ({env})" if env else ""))
    return resolved.strip()


class SearchConfig(StrictModel):
    provider: Literal["searxng", "tavily"] = "searxng"
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str | None = None
    engine: str = Field("duckduckgo", pattern=r"^[a-zA-Z0-9 _-]{1,80}$")
    language: str = "auto"
    max_results: int = Field(8, ge=1, le=20)
    timeout_seconds: float = Field(30, gt=0, le=120)
    retries: int = Field(1, ge=0, le=1)
    response_bytes: int = Field(2 * 1024**2, ge=1024, le=8 * 1024**2)
    max_credits_per_job: int = Field(10, ge=1, le=10)

    @model_validator(mode="after")
    def consistent(self):
        if self.base_url and self.base_url_env:
            raise ValueError("choose either an explicit search URL or its environment reference")
        for name in (self.base_url_env, self.api_key_env):
            if name is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("invalid environment variable reference")
        if self.provider == "tavily":
            if "engine" in self.model_fields_set and self.engine != "tavily":
                raise ValueError("Tavily requires engine: tavily")
            self.engine = "tavily"
            if "retries" not in self.model_fields_set:
                self.retries = 0
            if self.retries:
                raise ValueError("Tavily validation does not retry potentially charged searches")
            self.api_key_env = self.api_key_env or "TAVILY_API_KEY"
        elif self.api_key_env:
            raise ValueError("search API keys are supported only by the Tavily provider")
        return self

    def endpoint(self) -> str:
        if self.provider == "tavily":
            value = (
                _resolve(self.base_url, self.base_url_env, "Tavily base URL")
                if self.base_url or self.base_url_env
                else "https://api.tavily.com"
            )
            if value.rstrip("/") != "https://api.tavily.com":
                raise ConfigurationError("Tavily credentials may only be sent to https://api.tavily.com")
            return "https://api.tavily.com"
        return service_url(_resolve(self.base_url, self.base_url_env, "SearXNG base URL"))

    def resolve_api_key(self) -> SecretStr | None:
        if self.provider != "tavily":
            return None
        key = _resolve(None, self.api_key_env, "Tavily API key")
        if not key.isascii() or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in key):
            raise ConfigurationError("Tavily API key contains invalid characters")
        return SecretStr(key)


class ModelProfile(StrictModel):
    family: Literal["qwen", "deepseek"]
    base_url: str | None = None
    base_url_env: str | None = None
    model_id: str | None = None
    model_id_env: str | None = None
    api_key_env: str | None = None
    api: Literal["chat_completions"] = "chat_completions"
    output_mode: Literal["tool", "native_json", "prompted_json"]
    allow_remote_api: bool = False
    context_tokens: int = Field(32768, ge=2048, le=1_000_000)
    max_output_tokens: int = Field(4096, ge=128, le=32768)
    timeout_seconds: float = Field(120, gt=0, le=600)
    schema_retries: int = Field(1, ge=0, le=1)
    temperature: float | None = Field(None, ge=0, le=2)
    top_p: float | None = Field(None, gt=0, le=1)
    enable_thinking: bool | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    strict_tools: bool = False
    supports_json_object: bool = False
    multiple_system_messages: bool = False
    max_tokens_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    weight_revision: str | None = None
    quantization: str | None = None
    server: str | None = None
    server_version: str | None = None
    chat_template: str | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.max_output_tokens >= self.context_tokens:
            raise ValueError("max_output_tokens must leave context for the input")
        if self.base_url and self.base_url_env or self.model_id and self.model_id_env:
            raise ValueError("choose either an explicit value or its environment reference")
        for name in (self.base_url_env, self.model_id_env, self.api_key_env):
            if name is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("invalid environment variable reference")
        return self

    def resolve(self) -> ResolvedProfile:
        base_url = service_url(_resolve(self.base_url, self.base_url_env, "model base URL"))
        hostname = urlsplit(base_url).hostname or ""
        try:
            address = ipaddress.ip_address(hostname)
            local = address.is_private or address.is_loopback
        except ValueError:
            local = "." not in hostname or hostname.endswith((".local", ".lan", ".internal"))
        if not local and not self.allow_remote_api:
            raise ConfigurationError("public model endpoint requires explicit allow_remote_api: true")
        api_key = _resolve(None, self.api_key_env, "model API key") if self.api_key_env else "local-no-key"
        return ResolvedProfile(
            profile=self,
            base_url=base_url,
            model_id=_resolve(self.model_id, self.model_id_env, "model ID"),
            api_key=SecretStr(api_key),
        )


class ResolvedProfile(StrictModel):
    profile: ModelProfile
    base_url: str
    model_id: str
    api_key: SecretStr = Field(exclude=True, repr=False)

    def metadata(self) -> dict:
        fields = (
            "family",
            "api",
            "output_mode",
            "context_tokens",
            "max_output_tokens",
            "temperature",
            "top_p",
            "enable_thinking",
            "reasoning_effort",
            "weight_revision",
            "quantization",
            "server",
            "server_version",
            "chat_template",
            "max_tokens_parameter",
        )
        result = {key: getattr(self.profile, key) for key in fields}
        result.update(
            model_id=self.model_id,
            context_estimation=(
                "approximate per actual request JSON including messages/tools/schema: "
                "ASCII chars / 3 + non-ASCII chars * 2 + 1024 reserve + output limit"
            ),
        )
        return result


class RemoteConfig(StrictModel):
    """remote 収集モードの research 側設定．credential は env のみから解決する．"""

    manager_url: str
    requester_credential_env: str = Field("EPHY_MANAGER_REQUESTER_CREDENTIAL", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    target_worker_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    poll_interval_seconds: float = Field(2.0, gt=0, le=60)
    max_wait_seconds: float = Field(1800, gt=0, le=7200)
    deadline_seconds: float = Field(1200, gt=0, le=3600)
    allow_remote_api: bool = False
    allow_insecure_http: bool = False

    def endpoint(self) -> str:
        return manager_service_url(
            self.manager_url,
            allow_remote_api=self.allow_remote_api,
            allow_insecure_http=self.allow_insecure_http,
        )

    def resolve_credential(self) -> SecretStr:
        value = _resolve(None, self.requester_credential_env, "manager requester credential")
        return SecretStr(value)


class WorkerServiceConfig(StrictModel):
    """collector Worker 側設定（別の PC で起動する）．model は使わない．"""

    manager_url: str
    worker_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    credential_env: str = Field("EPHY_WORKER_CREDENTIAL", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    search: SearchConfig
    heartbeat_interval_seconds: float = Field(20, gt=0, le=300)
    poll_interval_seconds: float = Field(2, gt=0, le=60)
    allow_remote_api: bool = False
    allow_insecure_http: bool = False

    def endpoint(self) -> str:
        return manager_service_url(
            self.manager_url,
            allow_remote_api=self.allow_remote_api,
            allow_insecure_http=self.allow_insecure_http,
        )

    def resolve_credential(self) -> SecretStr:
        value = _resolve(None, self.credential_env, "worker credential")
        return SecretStr(value)


class ManagerSettings(StrictModel):
    """manager サービス本体の設定．worker credential は env 参照のみ．"""

    host: Literal["127.0.0.1", "::1", "localhost"] = "127.0.0.1"
    port: int = Field(8321, ge=1, le=65535)
    database_path: Path
    artifact_dir: Path
    requester_credential_env: str = Field("EPHY_MANAGER_REQUESTER_CREDENTIAL", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    worker_credentials_env: dict[str, str] = Field(default_factory=dict)
    lease_ttl_seconds: float = Field(45, gt=0, le=600)
    worker_offline_seconds: float = Field(90, gt=0, le=3600)
    max_artifact_bytes: int = Field(20 * 1024**2, ge=4096, le=64 * 1024**2)
    max_artifacts_per_job: int = Field(32, ge=1, le=128)

    @model_validator(mode="after")
    def consistent(self):
        for worker_id, env in self.worker_credentials_env.items():
            if not re.fullmatch(r"^[a-z0-9][a-z0-9._-]{2,79}$", worker_id):
                raise ValueError(f"invalid worker_id: {worker_id}")
            if not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$", env):
                raise ValueError(f"invalid environment variable name for worker {worker_id}")
        return self

    def resolved_worker_credentials(self) -> dict[str, str]:
        return {
            worker_id: _resolve(None, env, f"worker credential for {worker_id}")
            for worker_id, env in self.worker_credentials_env.items()
        }


class WorkerConfig(StrictModel):
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    search: SearchConfig | None = None
    limits: Limits = Field(default_factory=Limits)
    remote: RemoteConfig | None = None
    worker_service: WorkerServiceConfig | None = None
    manager: ManagerSettings | None = None


def load_config(path: str | Path) -> WorkerConfig:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return WorkerConfig.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        # ValidationError/YAML messages may contain raw credential values from an invalid config．
        raise ConfigurationError(f"cannot load Worker configuration ({type(exc).__name__})") from None
