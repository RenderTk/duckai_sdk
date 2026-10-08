"""Public input and output types for the SDK."""

import mimetypes
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duckai.errors import AttachmentError


@dataclass(frozen=True)
class Attachment:
    """An in-memory file; paths can also be passed directly to chat(files=[...])."""

    data: bytes
    filename: str
    mime_type: str = "application/octet-stream"

    @classmethod
    def from_bytes(cls, data: bytes, filename: str, mime_type: str | None = None):
        return cls(bytes(data), filename, mime_type or "application/octet-stream")

    @classmethod
    def from_path(cls, path: str | Path, *, max_bytes: int = 10 * 1024 * 1024):
        path = Path(path)
        with path.open("rb") as source:
            data = source.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise AttachmentError(
                413, {"error": "attachment_too_large", "message": "File too large"}
            )
        return cls(
            data, path.name, mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )


@dataclass
class ChatResponse:
    """Completed reply, original upstream events, and a reusable assistant message."""

    text: str = ""
    events: list[Any] = field(default_factory=list)
    done: bool = False
    model: str = "gpt-6-luna"
    _parts: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self._parts:
            message["parts"] = deepcopy(self._parts)
        return message

    def __str__(self) -> str:
        return self.text

    def _append(self, event: Any) -> str:
        self.events.append(deepcopy(event))
        if not isinstance(event, dict):
            return ""
        parts = event.get("parts")
        if isinstance(parts, list):
            # Parts with IDs may receive incremental state updates. Keep all opaque
            # reasoning/tool fields and merge updates without attempting decryption.
            for part in parts:
                if not isinstance(part, dict):
                    continue
                existing = next(
                    (p for p in self._parts if part.get("id") and p.get("id") == part["id"]), None
                )
                if existing is None:
                    self._parts.append(deepcopy(part))
                else:
                    existing.update(deepcopy(part))
        text = event.get("message")
        if isinstance(text, str):
            self.text += text
            return text
        return ""
