"""Explicit, loopback-only configuration for the desktop navigation client."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit


def application_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "EphyWorkerConsole"


def validate_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        valid_host = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
        if (
            parsed.scheme not in {"http", "https"}
            or not valid_host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char in value for char in "\r\n\x00")
        ):
            raise ValueError
        _ = parsed.port
    except (TypeError, ValueError):
        raise ValueError("endpoint must be an HTTP(S) loopback URL without credentials or query") from None
    return value


@dataclass(frozen=True)
class DesktopConfig:
    repository_path: Path
    base_revision: str = "HEAD"
    pi_executable: str = "pi"
    provider: str = "strata-local"
    model_id: str = "local-model"
    endpoint: str = "http://127.0.0.1:8081/v1"
    context_window: int = 32768
    artifact_root: Path | None = None
    models_file: Path | None = None
    karte_binary: Path | None = None
    karte_root: Path | None = None

    def __post_init__(self):
        for name in ("repository_path", "artifact_root", "models_file", "karte_binary", "karte_root"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, (str, Path)) or "\x00" in str(value):
                    raise ValueError(f"{name} must be a filesystem path")
                object.__setattr__(self, name, Path(value).expanduser().resolve())
        if self.artifact_root is None:
            object.__setattr__(self, "artifact_root", application_data() / "runs")
        for name in ("base_revision", "pi_executable", "provider", "model_id", "endpoint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 500 or any(c in value for c in "\r\n\x00"):
                raise ValueError(f"{name} must be a nonempty single-line string")
        if self.base_revision.startswith("-") or self.pi_executable.startswith("-"):
            raise ValueError("revision and executable cannot be command options")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", self.provider):
            raise ValueError("provider must be an ordinary provider identifier")
        if self.provider != "strata-local":
            raise ValueError("desktop v3 requires provider strata-local; update the explicit configuration")
        if self.model_id.startswith("-"):
            raise ValueError("live provider/model selection is invalid")
        if self.models_file is not None:
            raise ValueError("custom models_file is unsupported; the pinned Strata provider owns registration")
        if type(self.context_window) is not int or not 2048 <= self.context_window <= 2_000_000:
            raise ValueError("context_window must be between 2048 and 2000000")
        validate_endpoint(self.endpoint)
        if (self.karte_binary is None) != (self.karte_root is None):
            raise ValueError("karte_binary and karte_root must be configured together")

    @classmethod
    def from_file(cls, path: str | Path) -> DesktopConfig:
        source = Path(path).expanduser().resolve()
        if source.stat().st_size > 64 * 1024:
            raise ValueError("desktop configuration exceeds 64 KiB")
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or set(data) - {field.name for field in fields(cls)}:
            raise ValueError("desktop configuration contains unknown fields; arbitrary argv is unsupported")
        for name in ("repository_path", "artifact_root", "models_file", "karte_binary", "karte_root"):
            if data.get(name) is not None:
                if not isinstance(data[name], str):
                    raise ValueError(f"{name} must be a path string")
                value = Path(data[name]).expanduser()
                data[name] = value if value.is_absolute() else source.parent / value
        if "repository_path" not in data:
            raise ValueError("repository_path is required")
        return cls(**data)

    def models_document(self) -> dict:
        """Describe the pinned local registration without reading account credentials.

        Runtime registration is performed by the unchanged Strata extension.
        """
        return {"providers": {self.provider: {
            "baseUrl": self.endpoint,
            "api": "openai-completions",
            "apiKey": "local-no-auth",
            "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True,
                       "maxTokensField": "max_tokens", "thinkingFormat": "qwen-chat-template"},
            "models": [{"id": self.model_id, "name": self.model_id, "reasoning": True,
                        "contextWindow": self.context_window, "maxTokens": 16384}],
        }}}
