"""Coding model profile catalog independent from research model configuration．"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .coding_schema import CodingModelProfile, CodingProfileCatalog

DEFAULT_CODING_PROFILES = CodingProfileCatalog(
    profiles=[
        CodingModelProfile(
            profile_id="mock",
            provider="mock",
            model_id="deterministic-file-writer",
            server_type="mock",
            context_window=32768,
            reasoning_level="none",
            notes="Offline deterministic evaluation profile．No model process or endpoint is used．",
        ),
        CodingModelProfile(
            profile_id="qwen3-coder-30b-a3b",
            provider="llama_cpp",
            model_id="qwen3-coder-30b-a3b",
            server_type="llama.cpp",
            context_window=32768,
            reasoning_level="none",
            quantization="UD-Q4_K_XL",
            endpoint="http://127.0.0.1:8083/v1",
            pi_args=["--offline"],
            notes="Local llama.cpp code server shared with Ephy Runtime．",
        ),
    ]
)


def load_coding_profiles(path: Path | None = None) -> dict[str, CodingModelProfile]:
    if path is None:
        return DEFAULT_CODING_PROFILES.by_id()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        catalog = CodingProfileCatalog.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"cannot load coding profiles ({type(exc).__name__})") from None
    return catalog.by_id()
