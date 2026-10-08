"""Cross-language contracts live outside independently packaged SDKs."""

import base64
import json
from pathlib import Path

import httpx
import pytest

from duckai import Attachment, DuckAI, Model, Settings, list_models

ROOT = Path(__file__).resolve().parents[3]
CHAT = json.loads((ROOT / "protocol/fixtures/chat.json").read_text())
DOCUMENTS = json.loads((ROOT / "protocol/fixtures/documents.json").read_text())


def test_shared_chat_contract():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=CHAT["sse"].encode(),
        )

    with DuckAI(
        model=CHAT["model"],
        settings=Settings(_env_file=None, duckai_headers_json={"x-vqd-hash-1": "test"}),
        transport=httpx.MockTransport(handler),
    ) as ai:
        response = ai.chat(CHAT["prompt"])
        # Optional unset wire flags may be omitted or explicitly null.
        assert [{k: v for k, v in body.items() if v is not None} for body in sent] == [
            CHAT["request"]
        ]
        assert response.done and response.text == CHAT["text"]
        assert response.assistant_message["parts"] == CHAT["parts"]


def test_shared_catalogue():
    snapshot = json.loads((ROOT / "protocol/models.json").read_text())
    assert {m.value for m in Model} == {m["id"] for m in snapshot}
    for model, data in zip(list_models(), snapshot, strict=True):
        assert model.id == data["id"]
        assert model.access_tier == data["access_tier"]
        assert model.default_reasoning_effort == data["reasoning_efforts"][0]


@pytest.mark.parametrize("fixture", DOCUMENTS, ids=lambda f: f["filename"])
def test_shared_exported_document_contract(fixture):
    file = Attachment.from_bytes(base64.b64decode(fixture["data"]), fixture["filename"])
    with DuckAI() as ai:
        parts = ai.prepare_files([file], model=Model.MISTRAL_SMALL_4)
    assert len(parts) == 1 and parts[0]["type"] == "text"
    for expected in fixture["contains"]:
        assert expected in parts[0]["text"]
