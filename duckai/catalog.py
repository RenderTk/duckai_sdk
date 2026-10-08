"""Chat-model snapshot from Duck.ai's live frontend, verified October 7, 2026.

Access tiers describe Duck.ai entitlements, not permissions granted by this SDK.
Unknown model IDs remain valid for forward compatibility.
"""

from dataclasses import dataclass
from enum import StrEnum
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


MODELS = (
    ModelInfo(Model.GPT_6_LUNA.value, "GPT-6 Luna", "OpenAI", "For everyday questions and ideas"),
    ModelInfo(
        Model.GPT_5_4_MINI.value,
        "GPT-5.4 mini",
        "OpenAI",
        "A compact reasoning model",
        reasoning_efforts=("none", "low", "medium"),
    ),
    ModelInfo(
        Model.CLAUDE_HAIKU_4_5.value,
        "Claude Haiku 4.5",
        "Anthropic",
        "For writing and everyday conversations",
    ),
    ModelInfo(
        Model.MISTRAL_SMALL_4.value,
        "Mistral Small 4",
        "Mistral AI",
        "Open model · Text only",
        reasoning_efforts=("none",),
        supports_images=False,
        supports_pdf=False,
        supports_tools=False,
    ),
    ModelInfo(
        Model.GPT_OSS_120B.value,
        "gpt-oss 120B",
        "OpenAI",
        "Private reasoning · Text only",
        reasoning_efforts=("low",),
        supports_images=False,
        supports_pdf=False,
    ),
    ModelInfo(
        Model.GEMMA_4_31B.value,
        "Gemma 4 31B",
        "Google",
        "Private reasoning with images",
        supports_pdf=False,
        beta=True,
    ),
    ModelInfo(
        Model.GPT_5_6_TERRA.value,
        "GPT-5.6 Terra",
        "OpenAI",
        "Advanced everyday reasoning",
        access_tier="plus",
        reasoning_efforts=("none", "low", "medium"),
    ),
    ModelInfo(
        Model.CLAUDE_SONNET_4_6.value,
        "Claude Sonnet 4.6",
        "Anthropic",
        "Advanced writing and reasoning",
        access_tier="plus",
    ),
    ModelInfo(
        Model.GPT_5_6_SOL.value,
        "GPT-5.6 Sol",
        "OpenAI",
        "Deeper reasoning",
        access_tier="pro",
        reasoning_efforts=("none", "low", "medium"),
    ),
    ModelInfo(
        Model.CLAUDE_OPUS_4_8.value,
        "Claude Opus 4.8",
        "Anthropic",
        "Complex reasoning tasks",
        access_tier="pro",
        reasoning_efforts=("none", "low", "medium"),
    ),
)


def list_models(*, include_subscriber: bool = True) -> tuple[ModelInfo, ...]:
    """Return the bundled catalogue without network access or browser startup."""
    return tuple(m for m in MODELS if include_subscriber or m.access_tier == "free")


def get_model(model: str) -> ModelInfo | None:
    """Look up a wire ID; return None for custom or future models."""
    return next((m for m in MODELS if m.id == model), None)
