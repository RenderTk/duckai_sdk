from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    # Preserve encrypted reasoning parts, tool fields, and future Duck.ai fields.
    model_config = ConfigDict(extra="allow")

    role: str = Field(min_length=1)
    content: str | list[dict[str, Any]] | None = ""
    parts: list[dict[str, Any]] | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "examples": [
                {
                    "model": "gpt-6-luna",
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello!"}]}],
                    "canUseTools": True,
                    "reasoningEffort": "none",
                }
            ]
        },
    )

    model: str = Field(default="gpt-6-luna", min_length=1)
    messages: list[Message] = Field(min_length=1)
    canUseTools: bool = True
    reasoningEffort: str = "none"
    canUseApproxLocation: bool | None = None
    canDelegateImageGeneration: bool | None = None
    canShowGreeting: bool = False
    durableStream: dict[str, Any] | None = None

    def upstream_payload(self) -> dict[str, Any]:
        payload = self.model_dump()
        # Don't invent a durable stream or add absent fields to historical messages.
        if self.durableStream is None and "durableStream" not in self.model_fields_set:
            payload.pop("durableStream", None)
        payload["messages"] = [message.model_dump(exclude_unset=True) for message in self.messages]
        return payload


class ImageAttachment(BaseModel):
    type: Literal["image"] = "image"
    mimeType: Literal["image/webp"] = "image/webp"
    image: str = Field(description="Resized image as a data:image/webp;base64,... URL")


class PDFAttachment(BaseModel):
    type: Literal["file"] = "file"
    mimeType: Literal["application/pdf"] = "application/pdf"
    encoding: Literal["base64"] = "base64"
    filename: str
    content: str = Field(description="The original PDF bytes encoded as base64")


class PreparedAttachments(BaseModel):
    model: str
    content: list[ImageAttachment | PDFAttachment]
