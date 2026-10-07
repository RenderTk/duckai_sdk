"""Versioned, atomic local persistence; no Qt or network dependency."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from duckai import Message


def now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Turn:
    prompt: str
    answer: str = ""
    files: list[str] = field(default_factory=list)
    model: str = "gpt-6-luna"
    status: str = "pending"
    error: str = ""
    # Index into canonical SDK history before this turn. Preserves opaque tool
    # fields and native attachment content when regenerating a completed reply.
    history_start: int = 0
    request_message: dict | None = None
    created: str = field(default_factory=now)


@dataclass
class Chat:
    id: str = field(default_factory=lambda: uuid4().hex)
    title: str = "New chat"
    model: str = "gpt-6-luna"
    pinned: bool = False
    created: str = field(default_factory=now)
    updated: str = field(default_factory=now)
    turns: list[Turn] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    draft: str = ""
    draft_files: list[str] = field(default_factory=list)

    def touch(self):
        self.updated = now()

    def markdown(self) -> str:
        lines = [f"# {self.title}", "", f"Created: {self.created}", ""]
        for turn in self.turns:
            lines.extend(["## You", "", turn.prompt, ""])
            if turn.files:
                lines.extend(["Attachments: " + ", ".join(Path(f).name for f in turn.files), ""])
            lines.extend([f"## Duck.ai · {turn.model}", "", turn.answer, ""])
            if turn.status != "complete":
                lines.extend([f"[{turn.status}: {turn.error or 'Reply interrupted'}]", ""])
        return "\n".join(lines)


def decode_chat(data: dict) -> Chat:
    """Validate imports before they can become an SDK request."""
    if not isinstance(data, dict):
        raise ValueError("A conversation must be a JSON object")
    fields = {f for f in Chat.__dataclass_fields__ if f not in {"turns", "history"}}
    values = {key: value for key, value in data.items() if key in fields}
    for key, value in values.items():
        if key == "pinned":
            if not isinstance(value, bool):
                raise ValueError("Invalid pinned flag")
        elif key == "draft_files":
            if not isinstance(value, list) or any(not isinstance(p, str) for p in value):
                raise ValueError("Invalid draft attachments")
        elif not isinstance(value, str):
            raise ValueError(f"Invalid conversation field: {key}")
    chat = Chat(**values)
    if not chat.id or not chat.model.strip():
        raise ValueError("Conversation ID and model must not be empty")
    raw_history = data.get("history", [])
    if not isinstance(raw_history, list):
        raise ValueError("Invalid SDK history")
    chat.history = [Message.model_validate(m).model_dump(exclude_unset=True) for m in raw_history]
    raw_turns = data.get("turns", [])
    if not isinstance(raw_turns, list):
        raise ValueError("Invalid conversation turns")
    for raw in raw_turns:
        if not isinstance(raw, dict):
            raise ValueError("Invalid turn")
        turn = Turn(**{k: v for k, v in raw.items() if k in Turn.__dataclass_fields__})
        if any(
            not isinstance(getattr(turn, key), str)
            for key in ("prompt", "answer", "model", "status", "error", "created")
        ):
            raise ValueError("Invalid turn text")
        if not isinstance(turn.files, list) or any(not isinstance(p, str) for p in turn.files):
            raise ValueError("Invalid attachments")
        if (
            type(turn.history_start) is not int
            or not 0 <= turn.history_start <= len(chat.history)
            or turn.status not in {"pending", "complete", "stopped", "error"}
        ):
            raise ValueError("Invalid turn state")
        if turn.request_message is not None:
            turn.request_message = Message.model_validate(turn.request_message).model_dump(
                exclude_unset=True
            )
            if turn.request_message["role"] != "user":
                raise ValueError("Replay message must be from the user")
        if turn.status == "complete" and (
            turn.history_start + 1 >= len(chat.history)
            or chat.history[turn.history_start]["role"] != "user"
            or chat.history[turn.history_start + 1]["role"] != "assistant"
        ):
            raise ValueError("Completed turn is missing its SDK history")
        if turn.status == "pending":
            turn.status, turn.error = "stopped", "Interrupted when the application closed."
        chat.turns.append(turn)
    return chat


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "workspace.json"
        self.chats: list[Chat] = []
        self.preferences = {
            "theme": "light",
            "model": "gpt-6-luna",
            "timeout": 120,
            "tools": True,
            "reasoning": "none",
            "browser_executable": "",
            "browser_cdp_url": "",
            "headless": True,
        }
        self.warning = ""
        # A corrupt original must never be overwritten until a backup exists.
        self._write_blocked = False
        self.load()

    def load(self):
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("chats"), list):
                raise ValueError("Unsupported workspace format")
            chats = [decode_chat(c) for c in payload["chats"]]
            if len({c.id for c in chats}) != len(chats):
                raise ValueError("Duplicate conversation IDs")
            preferences = payload.get("preferences", {})
            if not isinstance(preferences, dict):
                raise ValueError("Invalid preferences")
            # Validate settings independently of files imported from outside the app.
            validated_preferences = self.preferences.copy()
            for key, default in self.preferences.items():
                value = preferences.get(key, default)
                if type(value) is not type(default):
                    raise ValueError(f"Invalid setting: {key}")
                validated_preferences[key] = value
            if validated_preferences["theme"] not in {"light", "dark", "system"}:
                raise ValueError("Invalid theme")
            if not 10 <= validated_preferences["timeout"] <= 600:
                raise ValueError("Invalid timeout")
            self.preferences = validated_preferences
            # Earlier versions defaulted to a visible browser. Migrate that
            # inherited setting once; subsequent explicit user choices persist.
            if payload.get("browser_defaults_version", 0) < 1:
                self.preferences["headless"] = True
            self.chats = chats
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
            backup = self.path.with_name(f"workspace.corrupt-{uuid4().hex[:8]}.json")
            try:
                self.path.rename(backup)
                self.warning = f"History could not be loaded ({error}). Original saved at {backup}."
            except OSError:
                self._write_blocked = True
                self.warning = (
                    f"History could not be loaded ({error}). Original left at {self.path}."
                )

    def save(self):
        if self._write_blocked:
            raise OSError("Cannot save until the unreadable workspace is moved or repaired")
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "browser_defaults_version": 1,
            "preferences": self.preferences,
            "chats": [asdict(c) for c in self.chats],
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".workspace-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(payload, target, ensure_ascii=False, indent=2)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def import_chat(self, path: Path) -> Chat:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Expected a Duck.ai Desktop version 1 export")
        chat = decode_chat(payload["chat"])
        chat.id = uuid4().hex
        chat.touch()
        self.chats.append(chat)
        return chat

    @staticmethod
    def export_json(chat: Chat) -> str:
        return json.dumps({"version": 1, "chat": asdict(chat)}, ensure_ascii=False, indent=2)
