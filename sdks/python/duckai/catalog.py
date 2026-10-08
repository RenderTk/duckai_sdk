"""Chat-model snapshot from Duck.ai's live frontend, verified October 7, 2026.

Access tiers describe Duck.ai entitlements, not permissions granted by this SDK.
Unknown model IDs remain valid for forward compatibility.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from typing import Literal


class Model(StrEnum):
    GPT_6_LUNA = "gpt-6-luna"
    GPT_5_4_MINI = "gpt-5.4-mini"
    CLAUDE_HAIKU_4_5 = "claude-haiku-4-5"
    MISTRAL_SMALL_4 = "mistral-small-2603"
    GPT_OSS_120B = "tinfoil/gpt-oss-120b"
    GEMMA_4_31B = "tinfoil/gemma4-31b"
    GPT_5_6_TERRA = "gpt-5.6-terra"
    CLAUDE_SONNET_4_6 = "claude-sonnet-4-6"
    GPT_5_6_SOL = "gpt-5.6-sol"
    CLAUDE_OPUS_4_8 = "claude-opus-4-8"


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    provider: str
    description: str
    access_tier: Literal["free", "plus", "pro"] = "free"
    reasoning_efforts: tuple[str, ...] = ("none", "low")
    supports_images: bool = True
    supports_pdf: bool = True
    supports_tools: bool = True
    beta: bool = False

    @property
    def default_reasoning_effort(self) -> str:
        return self.reasoning_efforts[0]


_DATA = json.loads(files("duckai").joinpath("_catalog_data.json").read_text())
MODELS = tuple(
    ModelInfo(
        **{
            **{
                k: v
                for k, v in item.items()
                if k not in {"constant", "attachment_profile", "reasoning_efforts"}
            },
            "reasoning_efforts": tuple(item["reasoning_efforts"]),
        }
    )
    for item in _DATA
)
NATIVE_PROFILES = {
    item["id"]: item["attachment_profile"] for item in _DATA if item["attachment_profile"]
}


def list_models(*, include_subscriber: bool = True) -> tuple[ModelInfo, ...]:
    """Return the bundled catalogue without network access or browser startup."""
    return tuple(m for m in MODELS if include_subscriber or m.access_tier == "free")


def get_model(model: str) -> ModelInfo | None:
    """Look up a wire ID; return None for custom or future models."""
    return next((m for m in MODELS if m.id == model), None)
