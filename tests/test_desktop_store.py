"""Durable history and import validation do not require Qt or a live service."""

import json
from dataclasses import asdict

import pytest

from example.store import Chat, Store, Turn, decode_chat


def test_workspace_roundtrip_preserves_native_history_and_drafts(tmp_path):
    history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Read this"},
                {"type": "file", "content": "base64-payload", "filename": "report.pdf"},
            ],
        },
        {
            "role": "assistant",
            "content": "A summary",
            "parts": [
                {"type": "reasoning", "encryptedText": "opaque"},
            ],
        },
    ]
    store = Store(tmp_path)
    chat = Chat(
        title="Research",
        pinned=True,
        history=history,
        draft="Follow up",
        draft_files=["/tmp/next.pdf"],
        turns=[
            Turn(prompt="Read this", answer="A summary", status="complete"),
        ],
    )
    store.chats.append(chat)
    store.save()
    restored = Store(tmp_path)
    assert asdict(restored.chats[0]) == asdict(chat)
    assert restored.path.stat().st_mode & 0o777 == 0o600
    export = tmp_path / "export.json"
    export.write_text(Store.export_json(chat))
    imported = restored.import_chat(export)
    assert imported.id != chat.id
    assert imported.history == history
    assert imported.draft == "Follow up"
    assert "## Duck.ai" in imported.markdown()


def test_interrupted_reply_is_visible_but_not_added_to_history(tmp_path):
    store = Store(tmp_path)
    store.chats.append(Chat(turns=[Turn(prompt="Hello", answer="Partial")]))
    store.save()
    restored = Store(tmp_path)
    assert restored.chats[0].turns[0].status == "stopped"
    assert restored.chats[0].turns[0].answer == "Partial"
    assert restored.chats[0].history == []


def test_corrupt_workspace_is_preserved_before_new_saves(tmp_path):
    original = "{broken json"
    (tmp_path / "workspace.json").write_text(original)
    store = Store(tmp_path)
    assert store.warning and not store.chats
    backups = list(tmp_path.glob("workspace.corrupt-*.json"))
    assert len(backups) == 1 and backups[0].read_text() == original
    store.save()
    assert json.loads(store.path.read_text())["version"] == 1


@pytest.mark.parametrize(
    "data",
    [
        {"turns": [{"prompt": "Hi", "status": "complete"}], "history": []},
        {"turns": [{"prompt": "Hi", "files": "not-a-list"}]},
        {"turns": [{"prompt": "Hi", "history_start": -1}]},
        {"turns": [{"prompt": "Hi", "request_message": {"role": "assistant"}}]},
        {"history": [{"content": "missing-role"}]},
    ],
)
def test_import_rejects_invalid_history_before_it_reaches_sdk(data):
    with pytest.raises(ValueError):
        decode_chat(data)


def test_invalid_preferences_do_not_partially_replace_defaults(tmp_path):
    (tmp_path / "workspace.json").write_text(
        json.dumps(
            {
                "version": 1,
                "chats": [],
                "preferences": {"theme": "unknown", "timeout": 4},
            }
        )
    )
    store = Store(tmp_path)
    assert store.preferences["theme"] == "light"
    assert store.preferences["timeout"] == 120


def test_old_visible_default_migrates_once_and_new_explicit_choice_persists(tmp_path):
    (tmp_path / "workspace.json").write_text(
        json.dumps(
            {
                "version": 1,
                "chats": [],
                "preferences": {"headless": False},
            }
        )
    )
    store = Store(tmp_path)
    assert store.preferences["headless"] is True
    store.preferences["headless"] = False
    store.save()
    assert Store(tmp_path).preferences["headless"] is False
