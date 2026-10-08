import asyncio
import io
import json

import httpx
import pytest
from PIL import Image

from duckai import (
    AsyncDuckAI,
    Attachment,
    AttachmentError,
    DuckAI,
    Model,
    Settings,
    get_model,
    list_models,
)


def test_catalogue_is_available_without_browser_or_network():
    models = list_models()
    assert len(models) == 10 and len({m.id for m in models}) == 10
    assert {m.id for m in models} == {m.value for m in Model}
    assert len(list_models(include_subscriber=False)) == 6
    assert get_model(Model.MISTRAL_SMALL_4).id == "mistral-small-2603"
    assert get_model("future-model") is None
    with DuckAI() as ai:
        assert ai.list_models() == models
        assert ai._client._tokens._process is None

    async def check():
        async with AsyncDuckAI() as ai:
            assert ai.list_models(include_subscriber=False) == list_models(include_subscriber=False)

    asyncio.run(check())


@pytest.mark.parametrize("info", list_models(), ids=lambda m: m.id)
def test_every_model_routes_streams_and_followups_with_correct_defaults(info):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == info.id
        assert body["reasoningEffort"] == info.default_reasoning_effort
        assert body["canUseTools"] == info.supports_tools
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"message":"OK"}\n\ndata: [DONE]\n\n',
        )

    with DuckAI(
        Model(info.id),
        settings=Settings(_env_file=None, duckai_headers_json={"x-vqd-hash-1": "test"}),
        transport=httpx.MockTransport(handler),
    ) as ai:
        chat = ai.conversation()
        assert chat.chat("Hello").text == "OK"
        with chat.stream("Follow up") as stream:
            assert "".join(stream) == "OK"
        assert len(requests[-1]["messages"]) == 3
        assert len(chat.messages) == 4


def test_text_only_models_and_gemma_attachment_capabilities():
    data = io.BytesIO()
    Image.new("RGB", (32, 32)).save(data, "PNG")
    image = Attachment.from_bytes(data.getvalue(), "image.png", "image/png")
    with DuckAI() as ai:
        for model in (Model.MISTRAL_SMALL_4, Model.GPT_OSS_120B):
            limits = ai.attachment_limits(model)
            assert not limits["image_supported"] and not limits["pdf_supported"]
            with pytest.raises(AttachmentError):
                ai.prepare_files([image], model=model)
        assert ai.prepare_files([image], model=Model.GEMMA_4_31B)[0]["type"] == "image"
        assert not ai.attachment_limits(Model.GEMMA_4_31B)["pdf_supported"]


def test_reasoning_errors_are_local_and_future_ids_remain_supported():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"message":"OK"}\n\ndata: [DONE]\n\n',
        )

    with DuckAI(
        settings=Settings(_env_file=None, duckai_headers_json={"x-vqd-hash-1": "test"}),
        transport=httpx.MockTransport(handler),
    ) as ai:
        with pytest.raises(ValueError, match="supports reasoning efforts: low"):
            ai.chat("Hello", model=Model.GPT_OSS_120B, reasoning_effort="none")
        assert not requests
        assert ai.chat("Hello", model="future-model", reasoning_effort="future-effort").text == "OK"
        assert requests[0]["model"] == "future-model"
