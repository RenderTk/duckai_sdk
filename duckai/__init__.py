"""Duck.ai from Python, with no API server to run."""

from duckai.catalog import Model, ModelInfo, get_model, list_models
from duckai.client import AsyncChatStream, AsyncDuckAI
from duckai.config import Settings
from duckai.conversation import AsyncConversation
from duckai.errors import (
    AttachmentError,
    ChallengeError,
    DuckAIError,
    IncompleteResponseError,
    RateLimitError,
)
from duckai.models import Message
from duckai.sync import ChatStream, Conversation, DuckAI
from duckai.types import Attachment, ChatResponse

__version__ = "0.2.0"
__all__ = [
    "AsyncChatStream",
    "AsyncConversation",
    "AsyncDuckAI",
    "Attachment",
    "AttachmentError",
    "ChallengeError",
    "ChatResponse",
    "ChatStream",
    "Conversation",
    "DuckAI",
    "DuckAIError",
    "IncompleteResponseError",
    "Message",
    "Model",
    "ModelInfo",
    "RateLimitError",
    "Settings",
    "get_model",
    "list_models",
]
